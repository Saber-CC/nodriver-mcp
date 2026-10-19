// Offline bridge to the unmodified upstream DevTools trace engine and formatters.
import {readFile} from 'node:fs/promises';
import {gunzipSync} from 'node:zlib';
import {pathToFileURL} from 'node:url';

try {
  const [enginePath, tracePath, insightSetId, insightName] = process.argv.slice(2);
  const engineURL = pathToFileURL(enginePath);
  const {DevTools} = await import(engineURL.href);
  const {parseTraceEventsFromBuffer} = await import(new URL('../processors/ChunkedTraceParser.js', engineURL).href);
  // Matches upstream devtools/DevtoolsUtils.ts. Insight models use localized strings.
  DevTools.I18n.DevToolsLocale.DevToolsLocale.instance({
    create: true,
    data: {navigatorLanguage: 'en-US', settingLanguage: 'en-US', lookupClosestDevToolsLocale: locale => locale},
  });
  DevTools.I18n.i18n.registerLocaleDataForTest('en-US', {});
  DevTools.Formatter.FormatterWorkerPool.FormatterWorkerPool.instance({
    forceNew: true, entrypointURL: new URL('./devtools-formatter-worker.js', engineURL).href,
  });
  let buffer = await readFile(tracePath);
  if (buffer[0] === 0x1f && buffer[1] === 0x8b) buffer = gunzipSync(buffer);
  const {events, metadata} = parseTraceEventsFromBuffer(buffer);
  if (!events.length) throw new Error('No trace events were found.');
  const engine = DevTools.TraceEngine.TraceModel.Model.createWithAllHandlers();
  await engine.parse(events, {metadata});
  const parsed = engine.parsedTrace();
  if (!parsed) throw new Error('The DevTools engine did not return a parsed trace.');
  const focus = DevTools.AgentFocus.fromParsedTrace(parsed);
  const insightSets = [...(parsed.insights?.values() ?? [])];
  const finite = value => typeof value === 'number' && Number.isFinite(value) ? value : null;
  const webVitals = insightSets.map(set => {
    const models = set.model;
    const interaction = models.INPBreakdown?.highPercentileInteractionEvent ?? models.INPBreakdown?.longestInteractionEvent;
    const fcp = parsed.data.PageLoadMetrics.metricScoresByFrameId.get(set.frameId)?.get(set.navigation)?.get('FCP');
    return {insight_set_id: set.id, url: String(set.url), frame_id: set.frameId,
      lcp_ms: finite(models.LCPBreakdown?.lcpMs),
      inp_ms: interaction ? finite(interaction.dur / 1000) : null,
      worst_interaction_ms: models.INPBreakdown?.longestInteractionEvent ? finite(models.INPBreakdown.longestInteractionEvent.dur / 1000) : null,
      cls: models.CLSCulprits ? Math.max(0, ...models.CLSCulprits.clusters.map(cluster => cluster.clusterCumulativeScore)) : null,
      fcp_ms: fcp ? finite(fcp.timing / 1000) : null};
  });
  const sets = insightSets.map(set => ({id: set.id, url: String(set.url), bounds: set.bounds,
    insights: Object.entries(set.model).map(([name, model]) => ({name, title: model.title, state: model.state, metric_savings: model.metricSavings})),
    errors: Object.fromEntries(Object.entries(set.modelErrors ?? {}).map(([name, error]) => [name, String(error.message ?? error)]))}));
  let output;
  if (insightSetId || insightName) {
    const set = parsed.insights?.get(insightSetId);
    if (!set) throw new Error(`Unknown insight_set_id ${insightSetId}; use an ID from performance_analyze_trace.`);
    const model = set.model[insightName];
    if (!model) throw new Error(`Unknown or unavailable insight_name ${insightName}; use a name from performance_analyze_trace.`);
    output = new DevTools.PerformanceInsightFormatter(focus, model).formatInsight();
  } else {
    output = new DevTools.PerformanceTraceFormatter(focus).formatTraceSummary();
  }
  const result = {ok: true, output, web_vitals: webVitals, insight_sets: sets,
    event_count: events.length, trace_bounds: parsed.data.Meta.traceBounds, url: parsed.data.Meta.mainFrameURL};
  await new Promise(resolve => process.stdout.write(JSON.stringify(result), resolve));
  process.exit(0);
} catch (error) {
  await new Promise(resolve => process.stdout.write(JSON.stringify({ok: false, error: String(error.message ?? error)}), resolve));
  process.exit(1);
}
