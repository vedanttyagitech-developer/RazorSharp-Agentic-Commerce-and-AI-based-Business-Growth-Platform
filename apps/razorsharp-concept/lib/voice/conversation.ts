/** The client mirrors lifecycle facts. This reducer grants no commerce authority. */
export type ConversationState = {
  connection:'closed'|'connecting'|'ready'|'lost';
  capture:'off'|'listening';
  intent:{id:number;stage:'queued'|'reasoning'|'closed';outcome?:string}|null;
  playback:{id:number;stage:'buffering'|'playing'}|null;
};
export type ConversationEvent =
 | {type:'connect'} | {type:'ready'} | {type:'disconnect'} | {type:'close'}
 | {type:'capture';active:boolean}
 | {type:'turn_opened';intent_id:number}
 | {type:'turn_reasoning';intent_id:number}
 | {type:'turn_closed';intent_id:number;outcome:string}
 | {type:'audio_queued';utterance_id:number}
 | {type:'audio_started';utterance_id:number}
 | {type:'audio_finished';utterance_id:number}
 | {type:'interrupt'};
export const initialConversation=():ConversationState=>({connection:'closed',capture:'off',intent:null,playback:null});
export function conversationTransition(state:ConversationState,event:ConversationEvent):ConversationState{
 switch(event.type){
  case 'connect':return {...initialConversation(),connection:'connecting'};
  case 'ready':return {...state,connection:'ready'};
  case 'close':return initialConversation();
  case 'disconnect':return {...state,connection:'lost',capture:'off',playback:null,intent:state.intent?{...state.intent,stage:'closed',outcome:'disconnected'}:null};
  case 'capture':return {...state,capture:event.active&&state.connection==='ready'?'listening':'off'};
  case 'turn_opened':return state.intent&&event.intent_id<=state.intent.id?state:{...state,intent:{id:event.intent_id,stage:'queued'}};
  case 'turn_reasoning':return state.intent?.id!==event.intent_id||state.intent.stage==='closed'?state:{...state,intent:{id:event.intent_id,stage:'reasoning'}};
  case 'turn_closed':return state.intent?.id!==event.intent_id||state.intent.stage==='closed'?state:{...state,intent:{id:event.intent_id,stage:'closed',outcome:event.outcome}};
  case 'audio_queued':return {...state,playback:{id:event.utterance_id,stage:'buffering'}};
  case 'audio_started':return state.playback?.id!==event.utterance_id?state:{...state,playback:{id:event.utterance_id,stage:'playing'}};
  case 'audio_finished':return state.playback?.id!==event.utterance_id?state:{...state,playback:null};
  case 'interrupt':return {...state,playback:null};
 }
}
export function conversationPhase(state:ConversationState):'idle'|'listening'|'transcribing'|'speaking'{
 if(state.connection!=='ready')return 'idle';
 if(state.playback?.stage==='playing')return 'speaking';
 if(state.playback?.stage==='buffering')return 'transcribing';
 if(state.intent&&state.intent.stage!=='closed')return 'transcribing';
 return state.capture==='listening'?'listening':'idle';
}
