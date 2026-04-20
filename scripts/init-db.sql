-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Create index for semantic similarity search
-- (Run after SQLAlchemy creates the tables via init_db())
-- Example: CREATE INDEX ON semantic_cache USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
