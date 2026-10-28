import {defineConfig} from 'vite';
import {readFileSync} from 'node:fs';
const version=JSON.parse(readFileSync(new URL('../VERSION.json',import.meta.url),'utf8')).version;
const apiTarget=process.env.GEWU_DEV_API_URL||process.env.GEWU_API_URL||'http://127.0.0.1:8765';
export default defineConfig({define:{__APP_VERSION__:JSON.stringify(version)},server:{proxy:{'/api':{target:apiTarget,changeOrigin:false}}}});
