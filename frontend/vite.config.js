import {defineConfig} from 'vite';
import {readFileSync} from 'node:fs';
const version=JSON.parse(readFileSync(new URL('../VERSION.json',import.meta.url),'utf8')).version;
export default defineConfig({define:{__APP_VERSION__:JSON.stringify(version)},server:{proxy:{'/api':'http://127.0.0.1:8765'}}});
