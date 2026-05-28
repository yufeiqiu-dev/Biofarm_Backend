## File Structure

```text
backend/
├── app/
│   ├── main.py                  # FastAPI application entry point
│   ├── api/v1/endpoints/
│   │   ├── admin_products.py    # Product CRUD routes (admin)
│   │   ├── admin_images.py      # Product image presign/confirm/delete routes (admin)
│   │   ├── admin_tags.py        # Tag CRUD routes (admin)
│   │   ├── tags.py              # Public tag listing
│   │   └── health.py
│   ├── core/config.py           # App configuration (pydantic-settings)
│   ├── dependencies/auth.py     # require_admin FastAPI dependency
│   ├── schemas/
│   │   ├── product.py           # Product/variant request & response schemas
│   │   ├── image.py             # Presigned URL, confirm, and delete schemas
│   │   └── tag.py               # Tag request & response schemas
│   ├── services/
│   │   ├── product_service.py   # Product business logic + S3 cleanup on delete
│   │   └── s3_service.py        # S3/CloudFront helpers (presigned URLs, delete)
│   ├── db/session.py            # Database session setup
│   └── models/
│       ├── product.py           # Product ORM model
│       ├── product_variant.py   # ProductVariant ORM model
│       └── tag.py               # Tag ORM model + product_tags join table
├── .env.example                 # Required environment variables
└── requirements.txt
```

---

## Local Setup

### 1. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

Copy `.env.example` to `.env` and fill in values:

```bash
cp .env.example .env
```

Required variables:

```
DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/oasis
AUTH_BYPASS=true

# AWS / S3 / CloudFront — see setup section below
S3_BUCKET_NAME=
AWS_REGION=
CLOUDFRONT_URL=
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
```

> `AUTH_BYPASS=true` disables Cognito JWT validation in development.

### 3. Start the server

```bash
uvicorn app.main:app --reload
```

API docs: http://127.0.0.1:8000/docs

---

## AWS S3 + CloudFront Setup

Product images are stored in S3 and served via CloudFront. Follow these steps to recreate the infrastructure.

### Step 1 — Create an S3 bucket

1. Go to S3 → **Create bucket**
2. Choose a unique name (e.g. `oasis-biofarm-images`) and a region
3. **Block all public access** — images will be served through CloudFront only
4. Leave versioning off

#### CORS policy (required for presigned PUT uploads from the browser)

In the bucket → **Permissions** → **Cross-origin resource sharing (CORS)**:

```json
[
  {
    "AllowedHeaders": ["*"],
    "AllowedMethods": ["GET", "PUT"],
    "AllowedOrigins": ["http://localhost:5174", "https://your-production-domain.com"],
    "ExposeHeaders": ["ETag"]
  }
]
```

### Step 2 — Create a CloudFront distribution

1. Go to CloudFront → **Create distribution**
2. **Origin domain**: select your S3 bucket from the dropdown
3. **Origin access**: choose **Origin access control (OAC)** → create a new OAC
4. After creation, CloudFront will prompt you to update the S3 bucket policy — copy and apply it
5. **Default cache behavior**: set **Viewer protocol policy** to "Redirect HTTP to HTTPS"
6. Note the distribution domain name (e.g. `d2c1lbzv12as4n.cloudfront.net`) — this is your `CLOUDFRONT_URL`

#### S3 bucket policy for CloudFront OAC

CloudFront generates this for you, but the pattern is:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowCloudFrontServicePrincipal",
      "Effect": "Allow",
      "Principal": {
        "Service": "cloudfront.amazonaws.com"
      },
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::YOUR-BUCKET-NAME/*",
      "Condition": {
        "StringEquals": {
          "AWS:SourceArn": "arn:aws:cloudfront::YOUR-ACCOUNT-ID:distribution/YOUR-DISTRIBUTION-ID"
        }
      }
    }
  ]
}
```

### Step 3 — Create an IAM user for the backend

1. Go to IAM → **Users** → **Create user**
2. Attach a custom inline policy with the minimum permissions needed:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3ImageAccess",
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:GetObject"
      ],
      "Resource": "arn:aws:s3:::YOUR-BUCKET-NAME/products/*"
    }
  ]
}
```

3. Under **Security credentials** → **Create access key** → choose "Application running outside AWS"
4. Copy `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` into `.env`

### Step 4 — Fill in `.env`

```
S3_BUCKET_NAME=oasis-biofarm-images
AWS_REGION=us-east-2
CLOUDFRONT_URL=https://d2c1lbzv12as4n.cloudfront.net
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
```

---

## Running Tests

```bash
.venv/bin/python -m pytest app/tests/ -q
```

All tests use an in-memory SQLite database and mock all S3 calls — no AWS credentials needed.

---

## API Endpoints

### Products (public)

| Method | Path | Auth |
|--------|------|------|
| GET | `/api/v1/products` | No — supports `?search=` and `?tag=` |
| GET | `/api/v1/products/{id}` | No |

### Products (admin)

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| GET | `/api/v1/admin/products` | Admin | |
| GET | `/api/v1/admin/products/{id}` | Admin | |
| POST | `/api/v1/admin/products` | Admin | |
| PUT | `/api/v1/admin/products/{id}` | Admin | Accepts optional `image_urls` to reorder |
| DELETE | `/api/v1/admin/products/{id}` | Admin | Cleans up S3 objects |

### Images (admin)

| Method | Path | Auth | Body | Notes |
|--------|------|------|------|-------|
| POST | `/api/v1/admin/products/{id}/images/presigned-url` | Admin | `{ extension }` | Returns signed S3 PUT URL + CloudFront URL |
| POST | `/api/v1/admin/products/{id}/images/confirm` | Admin | `{ image_url }` | Appends URL to product; idempotent |
| DELETE | `/api/v1/admin/products/{id}/images/` | Admin | `{ image_url }` | Removes from S3 + DB; idempotent (204 if not found) |

### Tags

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| GET | `/api/v1/tags` | No | Public tag listing |
| GET | `/api/v1/admin/tags` | Admin | |
| POST | `/api/v1/admin/tags` | Admin | 409 on duplicate name |
| DELETE | `/api/v1/admin/tags/{id}` | Admin | |

---

## Image Upload Flow

Images are uploaded directly from the browser to S3 using presigned URLs — the backend never handles image bytes.

1. **Presign** — `POST /images/presigned-url` → backend generates a signed PUT URL (5 min TTL) and the final CloudFront URL
2. **Upload** — browser PUTs the file directly to S3 using the presigned URL
3. **Confirm** — `POST /images/confirm` → backend appends the CloudFront URL to `product.image_urls`

Deletions and uploads in the admin UI are deferred until "Save Changes":
- Staged deletions are flushed first (individual URL-based DELETE calls)
- The product is then updated (metadata + reordered `image_urls`)
- Pending uploads are processed last (presign → PUT → confirm per file)

The first URL in `product.image_urls` is the primary/display image. Sending `image_urls` in a PUT reorders without changing S3 keys.
