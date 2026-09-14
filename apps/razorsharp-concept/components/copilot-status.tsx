'use client';

import { useEffect, useState } from 'react';
import { AlertCircle, WifiOff } from 'lucide-react';
import { useVoiceSession } from './voice-session';

/** Presentation only: browser connectivity is not a claim about API health. */
export function CopilotStatus() {
  const voice = useVoiceSession();
  const [offline, setOffline] = useState(false);
  useEffect(() => {
    const update = () => setOffline(!navigator.onLine);
    update();
    window.addEventListener('online', update);
    window.addEventListener('offline', update);
    return () => {
      window.removeEventListener('online', update);
      window.removeEventListener('offline', update);
    };
  }, []);
  const issue = offline ? 'Connection lost. Your draft stays here. Reconnect before sending.' : voice.notice;
  if (!issue) return null;
  return <div className="copilot-service-status" data-issue={!!issue}>
    {issue ? <div className="copilot-issue" role="alert">
      {offline ? <WifiOff size={16}/> : <AlertCircle size={16}/>}
      <span>{issue}</span>
    </div> : null}
  </div>;
}
