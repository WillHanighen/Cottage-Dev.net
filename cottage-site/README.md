# Cottage Site

A modern, lightweight FastAPI web app with server-rendered Jinja2 templates, HTMX for dynamic interactions, and Tailwind CSS for styling.

## Stack

- **FastAPI** - Fast, modern Python web framework
- **Uvicorn** - ASGI server for FastAPI
- **Gunicorn** - Production WSGI server
- **PostgreSQL** - Database with async SQLAlchemy + Alembic migrations
- **Redis** - Caching and pub/sub functionality
- **Jinja2** - Server-side templating engine
- **HTMX** - Dynamic HTML without JavaScript frameworks
- **Alpine.js** - Minimal JavaScript framework for interactions
- **Tailwind CSS** - Utility-first CSS framework
- **JWT Authentication** - Token-based auth system (to be implemented)

## Quickstart

### 1. Clone and enter the project directory:

```bash
git clone <repository-url>
cd cottage-site
```

### 2. Copy environment file and edit values as needed:

```bash
cp .env.example .env
```

Edit the `.env` file with your database credentials, Redis connection, and other configuration values.

### 3. Start infrastructure (PostgreSQL, Redis, optional Elasticsearch):

```bash
docker-compose up -d
```

### 4. Install Python dependencies:

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 5. Run database migrations:

```bash
alembic upgrade head
```

### 6. Start the development server:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Visit [http://localhost:8000](http://localhost:8000) to see your application.

## Development Notes

- Tailwind is loaded via CDN for now for simplicity. For production, consider compiling Tailwind CSS and purging unused styles.
- Migrations: Alembic is included. Models will be added and migrations configured in upcoming steps.
- Search: Meilisearch is optional and not yet wired in.

## Project Structure

``
cottage-site/
  app/
    core/            # config, db, redis clients
    web/             # routers, templates, static assets
    main.py          # FastAPI app
  docker-compose.yml
  requirements.txt
  .env.example
  README.md
``
