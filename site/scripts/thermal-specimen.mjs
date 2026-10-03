/** Add an analytical, multiframe teaching field to the gallery specimen. */
export function createThermalSpecimen(source) {
  if (source.toString('ascii', 0, 4) !== 'glTF' || source.readUInt32LE(4) !== 2) throw new Error('Expected a GLB 2.0 specimen.');
  const jsonLength = source.readUInt32LE(12);
  const document = JSON.parse(source.subarray(20, 20 + jsonLength).toString('utf8'));
  const binHeader = 20 + jsonLength;
  const original = source.subarray(binHeader + 8, binHeader + 8 + source.readUInt32LE(binHeader));
  const position = document.accessors[document.meshes[0].primitives[0].attributes.POSITION];
  const view = document.bufferViews[position.bufferView];
  const offset = (view.byteOffset ?? 0) + (position.byteOffset ?? 0);
  const count = position.count;
  const frames = 61;
  const data = Buffer.alloc(count * frames * 4);
  let min = Infinity;
  let max = -Infinity;
  // Gaussian solution of dT/dt = alpha * d²T/dy² on an infinite 1D medium.
  // alpha = 1 mm²/s, initial sigma = 3 mm, ambient = 20 °C, peak rise = 100 °C.
  for (let time = 0; time < frames; time++) {
    const variance = 9 + 2 * time;
    for (let node = 0; node < count; node++) {
      const height = original.readFloatLE(offset + node * (view.byteStride ?? 12) + 4) * 10;
      const temperature = 20 + 300 / Math.sqrt(variance) * Math.exp(-(height ** 2) / (2 * variance));
      data.writeFloatLE(temperature, (time * count + node) * 4);
      min = Math.min(min, temperature);
      max = Math.max(max, temperature);
    }
  }
  const accessor = document.accessors.length;
  document.bufferViews.push({ buffer: 0, byteOffset: original.length, byteLength: data.length });
  document.accessors.push({ bufferView: document.bufferViews.length - 1, componentType: 5126, count: count * frames, type: 'SCALAR', min: [min], max: [max] });
  document.scenes[0].extras.clio = {
    contract: 'clio.fea-mesh.v1', stage: 'analytical teaching field', topology: 'surface',
    fields: [{ name: 'temperature', label: 'Temperature', unit: '°C', location: 'node', count, frames, accessor }],
    frames: Array.from({ length: frames }, (_, time) => ({ label: `${time} s` })),
  };
  const binary = Buffer.concat([original, data]);
  document.buffers[0].byteLength = binary.length;
  const json = Buffer.from(JSON.stringify(document));
  const padded = Buffer.alloc(Math.ceil(json.length / 4) * 4, 0x20);
  json.copy(padded);
  const result = Buffer.alloc(12 + 8 + padded.length + 8 + binary.length);
  result.write('glTF', 0); result.writeUInt32LE(2, 4); result.writeUInt32LE(result.length, 8);
  result.writeUInt32LE(padded.length, 12); result.write('JSON', 16); padded.copy(result, 20);
  const start = 20 + padded.length;
  result.writeUInt32LE(binary.length, start); result.write('BIN\0', start + 4); binary.copy(result, start + 8);
  return result;
}
