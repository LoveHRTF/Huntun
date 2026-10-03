// Read Pi's own catalog without making model requests or returning credentials.
import {codexLoginProvider, reuseCliAuth} from './pi_auth.js';
import {homedir} from 'node:os';
import {join} from 'node:path';
const {ModelRuntime, DefaultResourceLoader, SettingsManager} = await import(process.argv[2]);
const runtime = await ModelRuntime.create({allowModelNetwork:false, signal:AbortSignal.timeout(8000)});
if (reuseCliAuth()) {
  runtime.registerNativeProvider(await codexLoginProvider(process.argv[2]));
  // The Claude adapter calls the unmodified CLI. Preflight checks auth/version/
  // capabilities without inference and never reads Claude's credentials.
  if (process.env.HUNTUN_PI_CLAUDE_EXTENSION) {
    const loader = new DefaultResourceLoader({cwd:process.cwd(),agentDir:process.env.PI_CODING_AGENT_DIR || join(homedir(),'.pi','agent'),
      settingsManager:SettingsManager.inMemory(), noExtensions:true,
      noSkills:true,noPromptTemplates:true,noThemes:true,noContextFiles:true,
      additionalExtensionPaths:[process.env.HUNTUN_PI_CLAUDE_EXTENSION]});
    await loader.reload();
    for (const {name,config} of loader.getExtensions().runtime.pendingProviderRegistrations) {
      runtime.registerProvider(name,config);
    }
  }
}
const available = await runtime.getAvailable(undefined,{signal:AbortSignal.timeout(8000)});
const requested = (process.env.HUNTUN_PI_MODELS || '').split(',').map(s=>s.trim()).filter(Boolean);
const rows = requested.length ? requested.map(id=>{
  const slash = id.indexOf('/');
  return runtime.getModel(id.slice(0,slash), id.slice(slash+1));
}).filter(Boolean) : available;
console.log(JSON.stringify(rows.map(m=>({id:m.id,provider:m.provider,name:m.name,
  contextWindow:m.contextWindow,cost:runtime.isUsingSubscription(m.provider) ? {} : m.cost,
  reasoning:m.reasoning,thinkingLevelMap:m.thinkingLevelMap}))));
