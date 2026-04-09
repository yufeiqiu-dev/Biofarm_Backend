## File Structure

```text
backend/
├── app/
│   ├── __init__.py
│   ├── main.py                  # FastAPI application entry point
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   ├── router.py            # Main API router
│   │   └── v1/
│   │       ├── __init__.py
│   │       └── endpoints/
│   │           ├── __init__.py
│   │           ├── health.py    # Health check endpoint
│   │           └── admin_products.py  # Admin product routes
│   │
│   ├── core/
│   │   ├── __init__.py
│   │   └── config.py            # App configuration and environment settings
│   │
│   ├── dependencies/
│   │   ├── __init__.py
│   │   └── auth.py              # Authentication / authorization dependencies
│   │
│   ├── schemas/
│   │   ├── __init__.py
│   │   └── product.py           # Pydantic request/response schemas
│   │
│   ├── services/
│   │   ├── __init__.py
│   │   └── product_service.py   # Business logic for products
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   ├── base.py              # Database base / model imports
│   │   └── session.py           # Database session setup
│   │
│   └── models/
│       ├── __init__.py          # SQLAlchemy models
│
├── tests/
│   ├── __init__.py              # Test package
│
├── .env.example                 # Example environment variables
└── requirements.txt             # Python dependencies