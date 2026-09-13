import { lazy, Suspense } from 'react';
import { createRoot } from 'react-dom/client';
import { MotionPolicy } from '@/components/continuity';
import '../app/globals.css';
import '../app/integration-workspace.css';
import '../app/motion.css';
import '../app/voice-native.css';
import '../app/identity.css';
import '../app/continuity.css';
import '../app/reserve.css';
import '../app/discovery.css';
import '../app/visual-system.css';
import '../app/command-experience.css';
import '../app/reserve-india.css';
import '../app/product-polish.css';
import '../app/platform-highlights.css';
import '../app/impact-deck.css';
import '../app/trust-boundaries.css';
import '../app/transaction-kernel.css';
import '../app/composer-prompts.css';
const pages = {
  '/': lazy(() => import('../app/page')),
  '/shop': lazy(() => import('../app/shop/page')),
  '/merchant': lazy(() => import('../app/merchant/page')),
  '/platform': lazy(() => import('../app/platform/page')),
};
const path = window.location.pathname.replace(/\/$/, '') || '/';
const Page = pages[path as keyof typeof pages];
createRoot(document.getElementById('root')!).render(
  <>
    <MotionPolicy />
    <Suspense fallback={<p role="status">Opening RazorSharp…</p>}>
      {Page ? <Page /> : <h1>Page not found</h1>}
    </Suspense>
  </>,
);
