// A per-process MCP registration: no project/global config files are overwritten.
import {closeCodexConnections, codexLoginProvider, reuseCliAuth} from './pi_auth.js';

export default async function(pi) {
  if (reuseCliAuth() && process.env.HUNTUN_PI_SDK) {
    pi.registerProvider(await codexLoginProvider(process.env.HUNTUN_PI_SDK));
    pi.on('session_shutdown', () => closeCodexConnections(process.env.HUNTUN_PI_SDK));
  }
  if (process.env.HUNTUN_PI_MCP_URL) {
    pi.registerMcpServer('huntun', {
      url:process.env.HUNTUN_PI_MCP_URL, exposure:'direct', timeout:300,
      description:'Huntun team discussion, task board, notes, commits and finish_cycle',
    });
  }
  pi.on('session_start', (_event,ctx) => {
    if (!pi.getCommands().some(c=>c.name === 'clm')) {
      throw new Error('Pi CLM did not load. Run pi install npm:@lolipopshock/pi-clm.');
    }
    // Pi reserves stdout for JSONL and redirects extension output during startup.
    process.stderr.write(JSON.stringify({type:'huntun_ready', sessionFile:ctx.sessionManager.getSessionFile(),
      model:ctx.model && {id:ctx.model.id,provider:ctx.model.provider,contextWindow:ctx.model.contextWindow}})+'\n');
  });
}
