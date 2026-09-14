import { mkdir, copyFile } from 'node:fs/promises';
for (const page of ['ecommerce-store', 'shop', 'merchant', 'platform']) {
  await mkdir('dist-mounted/' + page, { recursive: true });
  await copyFile(
    'dist-mounted/index.html',
    'dist-mounted/' + page + '/index.html',
  );
}
