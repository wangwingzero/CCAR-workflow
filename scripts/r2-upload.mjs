/**
 * R2 Upload Helper - Node.js script for uploading files to Cloudflare R2.
 * Uses @aws-sdk/client-s3 (Node.js BoringSSL bypasses OpenSSL 3.5.x TLS issues).
 *
 * Usage: node scripts/r2-upload.mjs <local_path> <r2_key> <content_type>
 *
 * Required env vars: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET
 */
import { S3Client, PutObjectCommand } from '@aws-sdk/client-s3';
import { readFileSync } from 'fs';

const [,, localPath, key, contentType] = process.argv;

if (!localPath || !key) {
  console.error('Usage: node r2-upload.mjs <local_path> <r2_key> <content_type>');
  process.exit(1);
}

const client = new S3Client({
  endpoint: `https://${process.env.R2_ACCOUNT_ID}.r2.cloudflarestorage.com`,
  region: 'auto',
  credentials: {
    accessKeyId: process.env.R2_ACCESS_KEY_ID,
    secretAccessKey: process.env.R2_SECRET_ACCESS_KEY,
  },
});

const body = readFileSync(localPath);
await client.send(new PutObjectCommand({
  Bucket: process.env.R2_BUCKET,
  Key: key,
  Body: body,
  ContentType: contentType || 'application/octet-stream',
}));
console.log('OK');
