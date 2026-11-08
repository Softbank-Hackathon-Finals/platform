import { app, BrowserWindow, ipcMain, nativeTheme, session, shell } from 'electron';
import { spawn } from 'node:child_process';
import path from 'node:path';
import type { AgentEvent, Settings, TurnOptions } from '../shared/types';
import { Agents, codexAuth } from './agent';
import { codexDefaultModel, codexModels } from './models';
import { awsProfile } from './accounts';
import { clearKey, keyStatus, saveKey } from './openai-key';
import { approval } from './approval';
import { loadSettings, saveSettings } from './settings';
import { SovaLink } from './sova';

declare const MAIN_WINDOW_VITE_DEV_SERVER_URL: string | undefined;
declare const MAIN_WINDOW_VITE_NAME: string;

const PROTOCOL = 'sova-console';
let win: BrowserWindow | null = null;
let settings: Settings;
const link = new SovaLink(() => settings);
const agents = new Agents(link, (e: AgentEvent) => win?.webContents.send('agent:event', e), () => settings.agentAuth);
link.confirm = (name, input) => agents.ask(name, input);

app.setAsDefaultProtocolClient(PROTOCOL);
nativeTheme.themeSource = 'light';

function createWindow() {
  win = new BrowserWindow({
    width: 1480, height: 940, minWidth: 1100, minHeight: 700, title: 'SOVA Console',
    titleBarStyle: 'hiddenInset', trafficLightPosition: { x: 16, y: 18 }, backgroundColor: '#FFF3D6',
    // backgroundThrottling off: Force Play keeps the live dashboard current behind other windows (Play still pauses itself when hidden).
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true, sandbox: true, nodeIntegration: false, backgroundThrottling: false },
  });
  // Text and layout scale together with the window: 1.0 at 1440 wide, 0.9 at the minimum, at most 1.08.
  const fit = () => { const [w] = win!.getContentSize(); win!.webContents.setZoomFactor(Math.min(1.08, Math.max(0.9, w / 1440))); };
  win.on('resize', fit);
  win.webContents.on('did-finish-load', fit);
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('https://')) void shell.openExternal(url);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (e) => e.preventDefault());
  if (MAIN_WINDOW_VITE_DEV_SERVER_URL) void win.loadURL(MAIN_WINDOW_VITE_DEV_SERVER_URL);
  else void win.loadFile(path.join(__dirname, `../renderer/${MAIN_WINDOW_VITE_NAME}/index.html`));
}

// Approval links from the MCP (sova-console://open?plan=..&approve=..) land in the app.
const deepLink = (url: string) => { if (url.startsWith(`${PROTOCOL}://`)) win?.webContents.send('deeplink', url); };
app.on('open-url', (e, url) => { e.preventDefault(); deepLink(url); });

ipcMain.handle('sova:call', (_e, name: string, args?: Record<string, unknown>) => link.call(name, args));
ipcMain.handle('sova:status', () => link.status());
ipcMain.handle('sova:resource', (_e, uri: string) => link.resource(uri));
ipcMain.handle('settings:get', () => settings);
ipcMain.handle('settings:save', (_e, patch: Partial<Settings>) => {
  settings = saveSettings(patch);
  link.reset();
  if ('agentAuth' in patch) agents.reset();
  return settings;
});
ipcMain.handle('auth:sso-login', () => new Promise((resolve) => {
  // Opens the browser for the person's own IAM Identity Center sign-in; nothing is stored by the app.
  const env = { ...process.env, PATH: `/opt/homebrew/bin:/usr/local/bin:${process.env.PATH ?? ''}` };
  const child = spawn('aws', ['sso', 'login', '--profile', settings.awsProfile], { env });
  child.on('error', () => resolve({ ok: false, message: 'aws CLI를 찾지 못했습니다.' }));
  child.on('exit', (code) => { link.reset(); resolve({ ok: code === 0, message: code === 0 ? 'SSO 로그인 완료' : `aws sso login 종료 코드 ${code}` }); });
}));
ipcMain.handle('agent:send', (_e, conv: string, text: string, opts: TurnOptions) => { void agents.send(conv, text, opts); });
ipcMain.handle('sova:prompts', () => link.prompts());
ipcMain.handle('agent:models', () => ({ models: codexModels(), defaultModel: codexDefaultModel() }));
ipcMain.handle('agent:auth', (_e, login: boolean) => codexAuth(login));
ipcMain.handle('approval', (_e, action: 'run-state' | 'approve-shift', planId: string, v?: number) => approval(settings, action, planId, v));
ipcMain.handle('agent:openai-key', async (_e, action: 'status' | 'save' | 'clear', key?: string) => {
  if (action === 'clear') { clearKey(); agents.reset(); }
  const result = action === 'save' && key ? await saveKey(key) : {};
  if ((result as { ok?: boolean }).ok) agents.reset();
  return { ...result, ...keyStatus() };
});
ipcMain.handle('accounts:aws', () => awsProfile(settings.awsProfile));
ipcMain.handle('sova:prompt', (_e, name: string, args: Record<string, string>) => link.prompt(name, args));
ipcMain.handle('agent:stop', (_e, conv: string) => agents.stop(conv));
ipcMain.handle('agent:confirm', (_e, id: string, allow: boolean) => agents.confirm(id, allow));

app.whenReady().then(async () => {
  settings = loadSettings();
  link.reset();
  session.defaultSession.setPermissionRequestHandler((_wc, _perm, cb) => cb(false));
  await link.start();
  if (!app.isPackaged) app.dock?.setIcon(path.join(app.getAppPath(), 'assets', 'icon.png'));
  createWindow();
});
app.on('window-all-closed', () => app.quit());
