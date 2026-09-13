 'use client';
import {Composer} from './concept';
import {VoiceSessionProvider} from './voice-session';
import './home-composer.css';
export function HomeComposer(){return <VoiceSessionProvider><div className="home-composer-dock" aria-label="Razor AI shopping assistant"><Composer judgeSuggestions placeholder="Hello judges, I’m RazorSharp AI. Ask me anything." onSend={text=>window.location.assign('/ecommerce-store/?ask='+encodeURIComponent(text))} onStartVoice={()=>window.location.assign('/ecommerce-store/?startVoice=1')}/></div></VoiceSessionProvider>}
