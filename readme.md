File Structure:
backend/
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── api/
│   │   ├── __init__.py
│   │   ├── router.py
│   │   └── v1/
│   │       ├── __init__.py
│   │       └── endpoints/
│   │           ├── __init__.py
│   │           ├── health.py
│   │           └── admin_products.py
│   ├── core/
│   │   ├── __init__.py
│   │   └── config.py
│   ├── dependencies/
│   │   ├── __init__.py
│   │   └── auth.py
│   ├── schemas/
│   │   ├── __init__.py
│   │   └── product.py
│   ├── services/
│   │   ├── __init__.py
│   │   └── product_service.py
│   ├── db/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   └── session.py
│   └── models/
│       ├── __init__.py
├── tests/
│   ├── __init__.py
├── .env.example
└── requirements.txt