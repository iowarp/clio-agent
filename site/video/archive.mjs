import {copyFile, mkdir, readFile, readdir, stat, writeFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {basename, dirname, resolve, sep} from 'node:path';
import {spawnSync} from 'node:child_process';

// Run from site/video. Copy originals without moving or rewriting them.
const destinationArgument = process.argv[2];
if (!destinationArgument) throw new Error('Usage: node archive.mjs ABSOLUTE_NEW_ARCHIVE_DIRECTORY');
const destination = resolve(destinationArgument);
const project = resolve('.');
const records = [];
const sha256 = (bytes) => createHash('sha256').update(bytes).digest('hex');

async function preserve(source, name) {
  const target = resolve(destination, name);
  if (!target.startsWith(destination + sep)) throw new Error(`Outside archive: ${name}`);
  const sourceBytes = await readFile(source);
  await mkdir(dirname(target), {recursive:true});
  // Exclusive creation protects existing archives against accidental replacement.
  await copyFile(source, target, 1);
  const targetBytes = await readFile(target);
  if (sha256(sourceBytes) !== sha256(targetBytes)) throw new Error(`Copy mismatch: ${name}`);
  const sourceInfo = await stat(source);
  records.push({path:name.replaceAll('\\','/'), source:resolve(source), bytes:sourceBytes.length,
    sha256:sha256(targetBytes), sourceModified:sourceInfo.mtime.toISOString()});
}

async function generated(name, contents) {
  const target = resolve(destination, name);
  await writeFile(target, contents, {flag:'wx'});
  records.push({path:name, source:null, bytes:Buffer.byteLength(contents), sha256:sha256(contents)});
}

const takes = resolve(project, 'public/takes');
const status = {
  'selection-continuous':'Rejected: 800-pixel recording trial.',
  'selection-take-02':'Rejected: 800-pixel recording trial.',
  'selection-take-03':'Rejected: cluttered presentation trial.',
  'selection-final':'Accepted: complete selection, compact reference, typing, working and answer.',
  'selection-20261003-r2':'Rejected: corrected pointer and framing captured, but the restored ARC event store failed to finalize the real response.',
  'selection-20261003-r4':'Rejected for publication: real selection and answer succeeded, but earlier renderer-repair text remained visible in the final composition.',
  'selection-20261003-r5':'Superseded: corrected continuous selection and answer, with an intervening permission approval. User requested a bypass-mode reshoot.',
  'selection-20261003-r6':'Accepted: continuous bypass-mode selection, 13 records, typing and both real peak timestamps; 23.1-second normal-speed edit.',
  'region-20261003-r3':'Rejected for publication: continuous attachment preview and real generated PNG, but permissions interrupted the take and the final image opened in a narrow canvas.',
  'region-20261003-r4':'Rejected for publication: bypass-mode workflow succeeded, but expanded artifact preview remained tiny. The image layout was subsequently fixed.',
  'region-20261003-r5':'Retained main workflow: continuous bypass-mode capture, attachment preview, real coastline rendering and artifact opening. Its initial figure mislabeled a peak position and required the recorded review request.',
  'region-20261003-r6-review':'Retained scientific review: actual request to label both peak observations, real agent work and opening the corrected output. Acceptance is documented in LESSONS.md.',
  'region-20261003-r7':'Superseded: real A2UI Image rendering, but the agent reused an earlier figure; not a fresh-generation demonstration.',
  'region-20261003-r8':'Rejected: stylized figure with invented coastline-like segments, lost transcript following, and wrong map fullscreen control.',
  'region-20261003-r8-geographic-review':'Retained actual scientific refinement with real Natural Earth geometry; rejected video because a development reload invalidated the UI.',
  'region-20261003-r9':'Retained: real new OpenStreetMap figure, attachment preview, Image rendering and fullscreen; rejected edit because the download card briefly reported an unavailable artifact.',
  'region-20261003-r10':'Rejected: registry loading was corrected, but a 154 ms delayed native scroll still lost following before the final Image view.',
  'region-20261003-r11':'Rejected: following stayed at the newest answer, but the agent reused the source map surface ID for its Image and placed the figure in an earlier message.',
  'region-20261003-r12':'Accepted workflow source: real capture, attachment preview, new inline Image and opening. First 110m figure is a coarse draft with a misplaced island label; retain its actual geographic refinement.',
  'region-20261003-r12-geographic-review':'Rejected: Escape did not exit the Image fullscreen dialog; attempted typing reached a hidden composer. No correction request was sent.',
  'region-20261003-r12-geographic-review2':'Accepted review source: actual request for detailed Natural Earth 10m coastlines, bounded geographic extent and both peak UTC labels. Its original fullscreen ending clipped the portrait; the edit uses the subsequent fitted result recording.',
  'region-20261003-r12-result-fitted':'Accepted fitted result: actual Image opening after portrait fullscreen viewport fitting was corrected; the whole figure is visible.',
  'region-final':'Rejected: selection toggle disabled capture; gesture panned the map.',
  'region-take-02':'Accepted main region source: full recording with actual waits and rejected draft.',
  'region-refinement-final':'Retained provenance: subsequent real coastline and layout refinement request.',
  'region-result-final':'Rejected: fullscreen transition cropped the encoded image.',
  'region-result-corrected':'Accepted result: exact exported PNG recorded in normal browser view.',
};
const takeCatalog = [];
for (const name of (await readdir(takes)).sort()) {
  if (name.endsWith('.mp4') && !name.includes('.limited.') && !name.includes('.h264.')) {
    const stem = basename(name, '.mp4');
    if (!status[stem]) throw new Error(`Classify raw take before archiving: ${name}`);
    await preserve(resolve(takes,name), `raw/${name}`);
    await preserve(resolve(takes,stem+'.json'), `raw/${stem}.json`);
    const probe = spawnSync('ffprobe',['-v','error','-show_entries','stream=codec_name,width,height,color_range,r_frame_rate,avg_frame_rate:format=duration','-of','json',resolve(takes,name)],{encoding:'utf8'});
    if (probe.error) throw probe.error;
    if (probe.status !== 0) throw new Error(probe.stderr);
    takeCatalog.push({source:`raw/${name}`, marks:`raw/${stem}.json`, status:status[stem], media:JSON.parse(probe.stdout)});
  } else if (name.endsWith('.limited.mp4')) {
    await preserve(resolve(takes,name), `normalized/${name}`);
  } else if (name.endsWith('.png')) {
    await preserve(resolve(takes,name), `evidence/capture/${name}`);
  }
}

for (const name of ['README.md','PROCEDURE.md','LESSONS.md','package.json','package-lock.json','capture.mjs','capture-client.mjs','diagnose-follow.mjs','pointer-guide.mjs','pointer-guide.test.mjs','command.mjs','prepare-region.mjs','take-selection.mjs','take-region.mjs','take-region-image.mjs','take-image-review.mjs','take-image-result.mjs','take-refinement.mjs','render-ffmpeg.mjs','render-ffmpeg.test.mjs','archive.mjs','selection.edit.json','region.edit.json','region-image.edit.json','selection.vtt','region.vtt','.gitignore']) {
  await preserve(resolve(project,name),`authoring/${name}`);
}
for (const name of await readdir(resolve(project,'src'))) {
  if ((await stat(resolve(project,'src',name))).isFile()) await preserve(resolve(project,'src',name),`authoring/src/${name}`);
}
for (const name of ['PROCEDURE.md','LESSONS.md']) await preserve(resolve(project,name),name);
for (const name of await readdir(resolve(project,'../public/media'))) {
  if (/^(clio-(select-reference|region-figure)\.(mp4|jpg|vtt)|dorian-bahamas-figure\.png|README\.md)$/.test(name))
    await preserve(resolve(project,'../public/media',name),`deliverables/${name}`);
}
for (const name of await readdir(resolve(project,'out'))) {
  if (name.endsWith('.png') || name.endsWith('.md5') || /^(normalize-.+|selection-render|region-render|site-build)\.log$/.test(name))
    await preserve(resolve(project,'out',name),`evidence/edit/${name}`);
  if (/^follow-audit.*\.json$/.test(name))
    await preserve(resolve(project,'out',name),`evidence/scroll/${name}`);
  if (/^region-r(?:7|8|9|10|11|12)-(?:session-transcript|agent-shell-commands)\.json$/.test(name) || /^region-r(?:8|9|11|12)-.*\.(png|py|zip)$/.test(name))
    await preserve(resolve(project,'out',name),`source-data/${name}`);
  if (/^region-image-r\d+-final\.mp4$/.test(name) || /^region-image-r\d+\.edit\.json$/.test(name))
    await preserve(resolve(project,'out',name),`review-edits/${name}`);
}
const review = 'D:/Libraries/Documents/projects/clio_develop_workspace/temp/a2ui-qa/website-final-review';
for (const name of await readdir(review)) {
  if (name.endsWith('.png')) await preserve(resolve(review,name),`evidence/website/${name}`);
}
await preserve(resolve(project,'../public/datasets/atlantic-hurricanes-noaa.csv'),'source-data/atlantic-hurricanes-noaa.csv');
const workspace = 'D:/Libraries/Documents/projects/clio_develop_workspace/temp/a2ui-qa/live-review-workspace';
await preserve(resolve(workspace,'dorian-bahamas-bend.png'),'source-data/dorian-bahamas-bend.png');
await preserve(resolve(workspace,'make_dorian_map.py'),'source-data/make_dorian_map.py');
const reshootWorkspace = 'D:/clioqa/widget-video-20261003-r4/workspace';
await preserve(resolve(project,'out/selection-session-transcript.json'),'source-data/selection-session-transcript.json');
await preserve(resolve(project,'out/region-session-transcript.json'),'source-data/region-session-transcript.json');
await preserve(resolve(reshootWorkspace,'dorian_bahamas_2019-08-29_to_09-04.csv'),'source-data/dorian_bahamas_2019-08-29_to_09-04.csv');
await preserve(resolve(reshootWorkspace,'dorian_bahamas_bend_peak_utc.png'),'source-data/reshoot-agent-map.png');
await preserve('D:/Libraries/Videos/clio_recordings/2026-10-03-reshoot-checkpoints/raw/region-r5-initial-agent-map.png','source-data/rejected-r5-peak-label.png');
await generated('take-catalog.json',JSON.stringify(takeCatalog,null,2)+'\n');
await generated('README.md',`# CLIO widget interaction recordings — 2026-10-03\n\nThis archive preserves the native continuous browser recordings before editing.\nEvery source was copied without modification and checked by SHA-256.\n\n- raw/: all ${takeCatalog.length} original takes and adjacent action timestamps, including rejected trials.\n- take-catalog.json: acceptance/rejection reasons, native dimensions, codec and duration.\n- normalized/: accepted editing copies; some historical region copies contain only their first 90 seconds.\n- authoring/: pinned capture/edit project, manifests and captions; no browser profiles or credentials.\n- deliverables/: published selection and region edits; inspect their media durations, posters and subtitles.\n- source-data/: real input CSV, exact final agent PNG and its plotting script.\n- evidence/: capture screenshots, decoded output frames, motion checks, render logs and website review.\n- PROCEDURE.md: recording, editing, reshoot, restoration and website recovery steps.\n- LESSONS.md: observed failures and decisions from this shoot.\n- manifest.json / manifest.sha256: copied source paths, sizes and integrity hashes.\n\nThe region figure follows actual coastline, label and layout refinements, disclosed\nin the published edit. The full raw main take preserves real waits and a rejected\ninitial proposal. Screenshots are review evidence, not the source of these videos.\n\nOriginal CLIO session: http://10.0.0.170:5176/workspaces/ws_f51a72e2d428/sessions/sess_16d7cd995a35\nTitle: Dorian near the Bahamas. Agent: Codex / Luna.\n\nBypass selection session: sess_0eb6bfc4ba32.\nBypass region and actual peak-label review: sess_4f4b5a89a780.\nPublished selection: 23.1 seconds at normal speed.\nPublished region: 63.5 seconds; real work intervals are labelled 8x, typing is 2x/3x.\nCurrent Image and geographic review session: sess_f8e88d435bc7.\nThe final Image opening is a separate actual recording after portrait fitting was corrected.\n\nWork from copies when reshooting or editing. Do not overwrite raw footage.\nThe archive is local preservation on this machine; it is not an off-machine backup.\n`);
records.sort((a,b)=>a.path.localeCompare(b.path));
await writeFile(resolve(destination,'manifest.json'),JSON.stringify({created:new Date().toISOString(),sourceProject:project,copyVerification:'All copied source bytes matched archive SHA-256',files:records},null,2)+'\n',{flag:'wx'});
await writeFile(resolve(destination,'manifest.sha256'),records.map(item=>`${item.sha256}  ${item.path}`).join('\n')+'\n',{flag:'wx'});
console.log(JSON.stringify({archive:destination,verifiedFiles:records.length,rawTakes:takeCatalog.length,totalBytes:records.reduce((sum,item)=>sum+item.bytes,0),takeDurations:takeCatalog.map(item=>({path:item.source,seconds:item.media.format.duration}))},null,2));
