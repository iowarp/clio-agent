export const widgetGroups = [
  { title: 'Data and space', names: ['clio.chart.v1', 'clio.data-table.v1', 'clio.map.v1', 'clio.raster-viewport.v1', 'clio.mesh-viewport.v1'] },
  { title: 'Decisions and work', names: ['clio.weather.v1', 'clio.message-draft.v1', 'clio.steps.v1', 'clio.action-card.v1', 'clio.approval.v1', 'clio.workflow.v1'] },
  { title: 'Information', names: ['clio.metric.v1', 'clio.status.v1', 'clio.progress.v1', 'clio.callout.v1', 'clio.artifact.v1', 'clio.code.v1', 'clio.diff.v1', 'clio.mermaid.v1'] },
  { title: 'Controls', names: ['Button', 'CheckBox', 'ChoicePicker', 'DateTimeInput', 'Slider', 'TextField', 'clio.slider.v1'] },
  { title: 'Layout and media', names: ['Column', 'Divider', 'Frame', 'Grid', 'Icon', 'Image', 'List', 'Modal', 'Row', 'Tabs', 'Text'] },
];

const labels = {
  'clio.chart.v1': 'Chart', 'clio.data-table.v1': 'Data table', 'clio.map.v1': 'Map',
  'clio.raster-viewport.v1': 'Raster viewport', 'clio.mesh-viewport.v1': '3D viewport',
  'clio.weather.v1': 'Weather', 'clio.message-draft.v1': 'Message draft',
  'clio.steps.v1': 'Steps', 'clio.action-card.v1': 'Action card',
  'clio.approval.v1': 'Approval', 'clio.workflow.v1': 'Workflow',
  'clio.metric.v1': 'Metric', 'clio.status.v1': 'Status',
  'clio.progress.v1': 'Progress', 'clio.callout.v1': 'Callout',
  'clio.artifact.v1': 'Artifact', 'clio.code.v1': 'Code',
  'clio.diff.v1': 'Diff', 'clio.mermaid.v1': 'Diagram',
  'clio.slider.v1': 'Numeric slider',
};

export const widgets = widgetGroups.flatMap((group) => group.names.map((name) => ({
  name,
  label: labels[name] ?? name.replace(/([a-z])([A-Z])/gu, '$1 $2'),
  slug: name === 'clio.slider.v1' ? 'numeric-slider' : name.startsWith('clio.') ? name.slice(5, -3).replaceAll('.', '-') : name.replace(/([a-z])([A-Z])/gu, '$1-$2').toLowerCase(),
  group: group.title,
})));

export function widgetFor(name) {
  return widgets.find((item) => item.name === name);
}

export const widgetUses = {
  'clio.chart.v1': 'Explore a measured relationship, compare categories, or follow observations through time. Choose a preset or a guarded Vega-Lite specification for the scientific question.',
  'clio.data-table.v1': 'Inspect exact rows and values, then filter or select records that need closer attention.',
  'clio.map.v1': 'Place sites, observations, shapes, or trajectories in geographic context and follow their spatial relationships.',
  'clio.raster-viewport.v1': 'Inspect a spatial field or image, including regions that can be captured for a follow-up question.',
  'clio.mesh-viewport.v1': 'Orbit a scientific 3D mesh and compare geometry or field results from different design states.',
  'clio.weather.v1': 'Present a forecast and the conditions that affect a planned activity.',
  'clio.message-draft.v1': 'Offer an editable message that a person can review before opening it in their mail app.',
  'clio.steps.v1': 'Show a short ordered process and where work stands within it.',
  'clio.action-card.v1': 'Put a clear next action beside the evidence or decision it follows.',
  'clio.approval.v1': 'Ask a person to confirm a consequential next step with the context visible.',
  'clio.workflow.v1': 'Trace a multi-step process as connected nodes and edges.',
  'clio.metric.v1': 'Highlight one quantitative result with its unit and context.',
  'clio.status.v1': 'Communicate the state of an operation or result at a glance.',
  'clio.progress.v1': 'Show how much of a bounded task has completed.',
  'clio.callout.v1': 'Make a warning, explanation, or important observation easy to find.',
  'clio.artifact.v1': 'Point to a file produced or used in the analysis, with a route to inspect it.',
  'clio.code.v1': 'Display source code or a command when the exact text matters.',
  'clio.diff.v1': 'Show what changed between two versions of text or code.',
  'clio.mermaid.v1': 'Explain a process or relationship as a readable diagram.',
  'clio.slider.v1': 'Let a person vary a numeric parameter and see the related view change.',
  Button: 'Trigger a named action from a compact control.',
  CheckBox: 'Turn a binary option on or off.',
  ChoicePicker: 'Choose one option from a defined set.',
  DateTimeInput: 'Enter a date or time for a query or decision.',
  Slider: 'Adjust a value across a bounded range.',
  TextField: 'Collect a short piece of editable text.',
  Column: 'Stack related content in reading order.',
  Divider: 'Separate groups without adding another heading.',
  Frame: 'Give a group of components a shared visual boundary.',
  Grid: 'Arrange comparable views or controls in columns.',
  Icon: 'Add a small visual cue to a label or action.',
  Image: 'Show a figure, photo, or generated visual beside the analysis.',
  List: 'Present related items that a person can scan quickly.',
  Modal: 'Present focused detail or a decision without losing the surrounding view.',
  Row: 'Place related components beside each other.',
  Tabs: 'Switch between alternative views of one subject.',
  Text: 'Add a heading, explanation, or concise result to a surface.',
};

// Plain-language documentation supplements fields whose catalog has no prose.
// These explanations do not add inputs or change the renderer contract.
export const widgetFieldHelp = {
  'clio.mesh-viewport.v1': {
    meshUri: 'Reference to a registered mesh artifact, such as artifact://artifact_specimen. The viewer loads the model from that artifact.',
    materialUri: 'Registered MTL companion file for an OBJ model.',
    format: 'The mesh file format. Set it when the file bytes do not identify the format unambiguously.',
    field: 'Name of the scalar result field to color by, as stored in the CLIO FEA mesh.',
    showField: 'Show or hide scalar coloring while keeping the geometry visible.',
    frame: 'Zero-based result frame. Bind it to a slider value to step through a simulation.',
    camera: 'Camera position, target, and orientation. Bind it to a data path to retain or coordinate the view.',
    syncGroup: 'Matching group names link the cameras and color scales of related 3D viewports.',
    thresholdField: 'Result field used to keep only geometry within a chosen value interval.',
    thresholdMin: 'Lower bound of the visible field interval. It can read a value from a control.',
    thresholdMax: 'Upper bound of the visible field interval. It can read a value from a control.',
    title: 'A short, descriptive title for the view.',
    upAxis: 'The axis treated as vertical when framing and orbiting the model.',
  },
  'clio.slider.v1': {
    label: 'Describe the quantity the person is changing.',
    value: 'Current numeric value, or two bounds in range mode. A bound data path lets other components read the same value.',
    min: 'Smallest allowed value.', max: 'Largest allowed value.',
    step: 'Distance between allowed values, including fractional increments.',
    range: 'Enable two handles for an interval instead of one value.',
    unit: 'Short unit shown beside the value, such as s or mm.',
  },
};
