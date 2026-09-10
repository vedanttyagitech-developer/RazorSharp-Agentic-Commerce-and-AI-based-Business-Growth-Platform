/**
 * Render settings.
 *
 * `setChromiumOpenGlRenderer('angle')` is what makes the text render identically on a Mac and
 * in CI; the default renderer antialiases differently and a pitch that is graded on a still
 * frame should not look different depending on where it was rendered.
 */

import {Config} from '@remotion/cli/config';

Config.setVideoImageFormat('jpeg');
Config.setOverwriteOutput(true);
Config.setChromiumOpenGlRenderer('angle');
Config.setEntryPoint('src/index.ts');
