import { describe, expect, it } from 'vitest';
import { invalidFields } from '../main/sova';
import { checkEnvironment, checkWorkload } from './register-checks';

const app = { name: 'demo-chat', repo_url: 'https://github.com/Softbank-Hackathon-Finals/demo-app', app_path: 'app', iac_path: 'deploy/',
  source_environment_id: 'env-aws', namespace: 'source-dev' };
// Built at run time: a token-shaped value for the check, never a literal that looks like a key.
const tokenLike = 'gh' + 'p_' + 'x'.repeat(36);
// A repository address with user info (`name@host`), joined at run time so the file holds no address-shaped text.
const withUser = (user: string) => `https://${user}${'@'}github.com/o/r`;

describe('registration checks', () => {
  it('names a pasted URL in connection_ref instead of sending it', () => {
    expect(checkEnvironment({ name: 'demo-chat', connection_ref: app.repo_url }).connection_ref).toMatch(/URL이 아니라/);
    expect(checkEnvironment({ name: 'demo-chat', connection_ref: 'bravo-source-collector' })).toEqual({});
    expect(checkEnvironment({ name: ' ', connection_ref: 'x' }).name).toBeDefined();
  });

  it('blocks credential-shaped values and userinfo in repository addresses', () => {
    expect(checkEnvironment({ name: 'a', connection_ref: tokenLike }).connection_ref).toMatch(/자격 값/);
    expect(checkWorkload({ ...app, repo_url: withUser(`user:${tokenLike}`) }).repo_url).toMatch(/자격 값|토큰/);
    expect(checkWorkload({ ...app, repo_url: withUser('user') }).repo_url).toMatch(/사용자 이름/);
    expect(checkWorkload({ ...app, repo_url: 'http://github.com/o/r' }).repo_url).toBeDefined();
  });

  it('keeps paths inside the repository and accepts a normal app', () => {
    expect(checkWorkload(app)).toEqual({});
    expect(checkWorkload({ ...app, app_path: '../etc' }).app_path).toBeDefined();
    expect(checkWorkload({ ...app, iac_path: '/abs' }).iac_path).toBeDefined();
    expect(checkWorkload({ ...app, namespace: 'has space' }).namespace).toBeDefined();
    expect(checkWorkload({ ...app, source_environment_id: '' }).source_environment_id).toBeDefined();
  });

  it('reduces a host validation error to field names', () => {
    const raw = "Error executing tool sova_register_environment: 1 validation error for sova_register_environmentArguments\nconnection_ref\n  String should match pattern '^x$' [type=string_pattern_mismatch, input_value='https://github.com/a/b', input_type=str]";
    expect(invalidFields(raw)).toEqual(['connection_ref']);
    expect(invalidFields('{"detail":{"code":"forbidden"}}')).toBeNull();
  });
});
