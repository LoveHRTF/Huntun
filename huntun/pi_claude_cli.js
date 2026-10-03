// Configure only the child CLI's builtin plugins; keep its binary/auth untouched.
import {spawn} from 'node:child_process';

export function claudeArgs(args) {
  const result = [...args];
  const index = result.indexOf('--settings');
  if (index >= 0 && result[index+1]?.startsWith('{')) {
    const settings = JSON.parse(result[index+1]);
    const plugins = {...settings.enabledPlugins};
    // Claude 2.1.286 added builtin plugin authoring to print-mode startup. The
    // pinned Pi adapter requires an empty customization surface for its MCP bridge.
    for (const name of ['agents-md','telemetry','plugin-authoring','mods-guide','tips']) {
      plugins[name+'@builtin'] = false;
      plugins['cc-plugin-'+name+'@builtin'] = false;
    }
    result[index+1] = JSON.stringify({...settings,enabledPlugins:plugins});
  }
  return result;
}

export function runClaude(executable) {
  const child = spawn(executable, claudeArgs(process.argv.slice(2)), {stdio:'inherit'});
  const handlers = new Map();
  for (const signal of ['SIGINT','SIGTERM']) {
    const handler = () => child.kill(signal);
    handlers.set(signal,handler);
    process.on(signal,handler);
  }
  child.on('error', () => { process.stderr.write('Unable to launch Claude Code.\n'); process.exit(1); });
  child.on('exit', (code,signal) => {
    for (const [name,handler] of handlers) process.off(name,handler);
    if (signal) process.kill(process.pid, signal); else process.exit(code ?? 1);
  });
}
