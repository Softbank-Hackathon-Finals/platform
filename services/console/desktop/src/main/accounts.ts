// Console entry points for the person's own accounts. AWS values come from the local AWS SSO profile
// (portal URL, account, role, region: not secrets); GCP and Tailscale are what the person types in.
import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

export interface AwsProfile { startUrl?: string; accountId?: string; roleName?: string; region?: string }

export function awsProfile(name: string): AwsProfile {
  let text = '';
  try { text = readFileSync(process.env.AWS_CONFIG_FILE ?? join(homedir(), '.aws', 'config'), 'utf8'); } catch { return {}; }
  const sections: Record<string, Record<string, string>> = {};
  let cur = '';
  for (const line of text.split('\n')) {
    const h = /^\[(.+)\]\s*$/.exec(line.trim());
    if (h) { cur = h[1]; sections[cur] = {}; continue; }
    const kv = /^([A-Za-z0-9_]+)\s*=\s*(.+)$/.exec(line.trim());
    if (kv && cur) sections[cur][kv[1]] = kv[2].trim();
  }
  const p = sections[`profile ${name}`] ?? {};
  const session = p.sso_session ? sections[`sso-session ${p.sso_session}`] ?? {} : {};
  return { startUrl: p.sso_start_url ?? session.sso_start_url, accountId: p.sso_account_id, roleName: p.sso_role_name, region: p.region };
}
