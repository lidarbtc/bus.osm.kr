import { createApp } from "../lib/app.js";
import { blobStore } from "../lib/store.js";

const handle = createApp({ env: process.env, store: blobStore() });

export const GET = (request) => handle(request);
export const POST = (request) => handle(request);
