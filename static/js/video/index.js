// static/js/video/index.js
//
// Public surface of the gallery's video editing frontend. app.js imports this
// for the module side effect of registering the editor, and gallery.js uses it
// to decide whether the Edit button opens the image editor or this one.

export { openVideoEditor, closeVideoEditor, isVideoUrl } from './videoEditor.js';
export { encodeLocally, isSupported as browserEncoderSupported } from './browserEncoder.js';
export * from './ops.js';
