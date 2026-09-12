'use client';

import {useEffect, useState} from 'react';
import {ArrowUpRight, ChevronLeft, ChevronRight, Pause, Play, Sparkles} from 'lucide-react';

const shopping = [
  ['Find products', 'Find milk and bread'],
  ['Product details', 'Tell me about the milk options'],
  ['Availability', 'Is the brown bread in stock?'],
  ['Add to cart', 'Add milk to my cart'],
  ['Add quantity', 'Add two packets of bread'],
  ['Change quantity', 'Change the milk quantity to two'],
  ['Remove an item', 'Remove bread from my cart'],
  ['Your cart', 'Show my cart and total'],
  ['Reserve Pay', 'Can I use Reserve Pay for this cart?'],
  ['Review', 'Take me to checkout'],
  ['Speak Hindi', 'मुझे दूध और ब्रेड दिखाओ'],
];
const checkout = [
  ['Your bill', 'Explain my bill'],
  ['Delivery', 'How much is delivery?'],
  ['Discounts', 'What discount is applied?'],
  ['Payment options', 'What are my payment options?'],
];

export function ComposerPrompts({compact, active, onSelect}: {
  compact: boolean; active: boolean; onSelect: (text: string) => void;
}) {
  const prompts = compact ? checkout : shopping;
  const [index, setIndex] = useState(0);
  const [paused, setPaused] = useState(false);
  const [hovered, setHovered] = useState(false);
  useEffect(() => {
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
    if (!active || paused || hovered || reduced.matches) return;
    const timer = window.setInterval(() => {
      if (document.hidden || reduced.matches || document.documentElement.dataset.motion === 'paused') return;
      setIndex(i => (i + 1) % prompts.length);
    }, 4500);
    return () => window.clearInterval(timer);
  }, [active, paused, hovered, prompts.length]);
  if (!active) return null;
  const current = index % prompts.length;
  const [label, prompt] = prompts[current];
  return <div className="composer-prompts" onMouseEnter={() => setHovered(true)} onMouseLeave={() => setHovered(false)}
    onFocusCapture={() => setHovered(true)} onBlurCapture={event => {if (!event.currentTarget.contains(event.relatedTarget)) setHovered(false)}}>
    <div className="composer-prompts-heading"><span><Sparkles size={13}/> Try asking Razor AI</span>
      <div className="composer-prompts-controls">
        <button type="button" aria-label="Previous suggestion" onClick={() => setIndex(i => (i + prompts.length - 1) % prompts.length)}><ChevronLeft size={14}/></button>
        <button type="button" aria-label={paused ? 'Resume suggestions' : 'Pause suggestions'} onClick={() => setPaused(p => !p)}>{paused ? <Play size={12}/> : <Pause size={12}/>}</button>
        <button type="button" aria-label="Next suggestion" onClick={() => setIndex(i => (i + 1) % prompts.length)}><ChevronRight size={14}/></button>
      </div>
    </div>
    <button type="button" key={prompt} className="composer-prompt-example" onClick={() => onSelect(prompt)}>
      <span><small>{label}</small><strong>“{prompt}”</strong></span><ArrowUpRight size={18}/>
    </button>
    <div className="composer-prompt-categories" aria-label="Things to try">{prompts.map(([category], i) =>
      <button type="button" key={category} aria-pressed={current === i} onClick={() => setIndex(i)}>{category}</button>)}</div>
  </div>;
}
