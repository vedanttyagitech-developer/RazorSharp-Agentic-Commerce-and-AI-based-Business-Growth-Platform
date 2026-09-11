'use client';

// Keeps the framework's own navigation module whole through the production bundler.
//
// THE DEFECT THIS EXISTS TO REMOVE
// --------------------------------
// `vinext` loads its navigation shim as a namespace and reads the functions off it only
// after an `await`, with the namespace arriving as one element of an array:
//
//     const [navigation, ...] = await Promise.all([loadNavigationModule(), ...]);
//     const { getPrefetchInterceptionContext, getPrefetchCache, ... } = navigation;
//
// Rollup cannot follow a namespace through an array destructured out of `Promise.all`, so
// it concludes those exports are unused and shakes them out. Development bundles nothing
// and is therefore fine, which is why this survived every local run: the defect exists
// only in a production build. The deployed bundle shipped a navigation chunk carrying 25
// of that module's ~100 exports, and none of the ones named above.
//
// What that looked like was not a router that fell back to a page load. It was a site
// where every internal link did nothing at all. The click handler ran, called
// `preventDefault`, reached `getPrefetchInterceptionContext(...)`, threw `f is not a
// function` -- and the browser did not navigate, because the default had already been
// cancelled. Nothing moved, nothing was logged where a visitor would see it, and the
// pages themselves were healthy: typing the same URL loaded them immediately.
//
// Handing the namespace to something the bundler cannot reason about is what keeps the
// exports. `window` is such a place: assigning to it is a side effect Rollup must
// preserve, and a namespace passed whole cannot have its members proved unused.
//
// This is a workaround for a framework bug, not a design. It can go when `vinext` stops
// routing that namespace through `Promise.all`, or when a release fixes it -- the app is
// on `1.0.0-beta.5` and betas up to `1.0.0-beta.9` exist, untested here.

import * as navigation from 'next/navigation';

declare global {
  interface Window {
    /** Not read by anything. Present so the bundler keeps the module it points at. */
    __vinextNavigation?: typeof navigation;
  }
}

if (typeof window !== 'undefined') {
  window.__vinextNavigation = navigation;
}

/**
 * Renders nothing, and exists to be rendered.
 *
 * The side effect above only runs if this module is in the client graph, and it is in the
 * client graph because the root layout mounts this. A bare `import` for side effects would
 * read as deletable to anyone tidying imports, which is exactly how this defect returns.
 */
export function KeepRouterExports(): null {
  return null;
}
