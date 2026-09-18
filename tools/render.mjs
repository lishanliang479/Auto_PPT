// 使用可用的本地Office渲染库检查模板和输出，不修改原始文件。
import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [source, output, moduleFile, selection] = process.argv.slice(2);
if (!source || !output || !moduleFile) {
  throw new Error('需要源文件、输出目录和本地artifact-tool模块路径');
}
const { FileBlob, PresentationFile } = await import(pathToFileURL(moduleFile).href);
await fs.mkdir(output, { recursive: true });
const deck = await PresentationFile.importPptx(await FileBlob.load(source));
const inspection = await deck.inspect({ kind: 'slide,textbox,image', maxChars: 2000000 });
await fs.writeFile(path.join(output, 'inspection.ndjson'), inspection.ndjson);
const slides = deck.slides.items;
const indices = selection ? selection.split(',').map(Number).map(n => n - 1) : slides.map((_, i) => i);
for (const index of indices) {
  const preview = await deck.export({ slide: slides[index], format: 'png', scale: 1 });
  await fs.writeFile(path.join(output, `slide-${index + 1}.png`), new Uint8Array(await preview.arrayBuffer()));
  console.log(`已渲染 ${index + 1}/${slides.length}`);
}
