import { contextBridge, ipcRenderer } from 'electron';
import type { AgentEvent, SovaApi } from '../shared/types';

const api: SovaApi = {
  call: (name, args) => ipcRenderer.invoke('sova:call', name, args),
  status: () => ipcRenderer.invoke('sova:status'),
  resource: (uri) => ipcRenderer.invoke('sova:resource', uri),
  settings: () => ipcRenderer.invoke('settings:get'),
  saveSettings: (patch) => ipcRenderer.invoke('settings:save', patch),
  ssoLogin: () => ipcRenderer.invoke('auth:sso-login'),
  agentSend: (conv, text, opts) => ipcRenderer.invoke('agent:send', conv, text, opts),
  prompts: () => ipcRenderer.invoke('sova:prompts'),
  models: () => ipcRenderer.invoke('agent:models'),
  agentAuth: (login) => ipcRenderer.invoke('agent:auth', login),
  awsProfile: () => ipcRenderer.invoke('accounts:aws'),
  prompt: (name, args) => ipcRenderer.invoke('sova:prompt', name, args),
  agentStop: (conv) => ipcRenderer.invoke('agent:stop', conv),
  agentConfirm: (id, allow) => ipcRenderer.invoke('agent:confirm', id, allow),
  onAgent: (cb) => {
    const h = (_: unknown, e: AgentEvent) => cb(e);
    ipcRenderer.on('agent:event', h);
    return () => { ipcRenderer.removeListener('agent:event', h); };
  },
  onDeepLink: (cb) => {
    const h = (_: unknown, url: string) => cb(url);
    ipcRenderer.on('deeplink', h);
    return () => { ipcRenderer.removeListener('deeplink', h); };
  },
};
contextBridge.exposeInMainWorld('sova', api);
