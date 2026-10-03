import React from 'react';
import {AbsoluteFill, Composition, OffthreadVideo, Sequence, interpolate, registerRoot, staticFile, useCurrentFrame} from 'remotion';

type Shot = {source?: string; from: number; to: number; speed?: number; zoom?: number; x?: number; y?: number; caption?: string; crop?: {x:number; y:number; width:number; height:number}};
type Edit = {source: string; shots: Shot[]};
const fps = 30;
const sample: Edit = {source: 'takes/selection-continuous.mp4', shots: [{from: 0, to: 5}]};

function RecordedShot({source, shot}: {source: string; shot: Shot}) {
  const frame = useCurrentFrame();
  const ramp = interpolate(frame, [0, 30], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  const scale = 1 + ((shot.zoom ?? 1) - 1) * ramp;
  const crop = shot.crop ?? {x:0,y:0,width:1280,height:870};
  const fit = Math.min(1280/crop.width,870/crop.height);
  return <AbsoluteFill style={{overflow: 'hidden', backgroundColor: '#f4f9f9'}}>
    <div style={{position:'absolute',left:(1280-crop.width*fit)/2,top:(870-crop.height*fit)/2,width:crop.width*fit,height:crop.height*fit,overflow:'hidden',transformOrigin:`${shot.x ?? 50}% ${shot.y ?? 50}%`,transform:`scale(${scale})`}}>
      <OffthreadVideo src={staticFile(source)} startFrom={Math.round(shot.from * fps)} playbackRate={shot.speed ?? 1} muted style={{position:'absolute',width:1280*fit,height:870*fit,left:-crop.x*fit,top:-crop.y*fit}} />
    </div>
    {shot.caption && <div style={{position:'absolute',left:24,bottom:24,maxWidth:690,padding:'10px 16px',borderRadius:8,background:'#063941ee',color:'white',fontFamily:'Arial, sans-serif',fontSize:21,lineHeight:1.3}}>{shot.caption}</div>}
  </AbsoluteFill>;
}

function DemoEdit({source, shots}: Edit) {
  let offset = 0;
  return <AbsoluteFill>{shots.map((shot, index) => {
    const length = Math.round((shot.to - shot.from) / (shot.speed ?? 1) * fps);
    const from = offset;
    offset += length;
    return <Sequence key={index} from={from} durationInFrames={length}><RecordedShot source={shot.source ?? source} shot={shot} /></Sequence>;
  })}</AbsoluteFill>;
}

registerRoot(() => <Composition id="DemoEdit" component={DemoEdit} width={1280} height={870} fps={fps} durationInFrames={150} defaultProps={sample} calculateMetadata={({props}) => ({durationInFrames: props.shots.reduce((sum,shot) => sum+Math.round((shot.to-shot.from)/(shot.speed ?? 1)*fps),0)})} />);
