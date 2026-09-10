/**
 * The two faces, loaded rather than hoped for.
 *
 * A CSS stack that merely *names* Inter renders in whatever the machine happens to have, so
 * the same composition looks different on a laptop and in CI -- which for a submitted video
 * means the frame a judge sees is not the frame that was designed. `@remotion/google-fonts`
 * ships the files and Remotion waits for them before painting, so the render is identical
 * everywhere and works with no network at render time.
 *
 * Only the weights actually used are requested; every extra weight is a font file the
 * renderer has to load on every one of 9000 frames.
 */

import {loadFont as loadInter} from '@remotion/google-fonts/Inter';
import {loadFont as loadMono} from '@remotion/google-fonts/JetBrainsMono';

const inter = loadInter('normal', {weights: ['400', '500', '600', '700'], subsets: ['latin']});
const mono = loadMono('normal', {weights: ['400', '500'], subsets: ['latin']});

export const FONT_SANS = `${inter.fontFamily}, system-ui, -apple-system, sans-serif`;
export const FONT_MONO = `${mono.fontFamily}, ui-monospace, SFMono-Regular, Menlo, monospace`;
