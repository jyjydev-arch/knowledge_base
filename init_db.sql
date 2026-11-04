-- ============================================================
-- AI 知识库 · PostgreSQL 建表脚本（快照）
--
-- 真源是 app/db/models.py，本文件由下面命令生成，请勿手工编辑：
--     app/.venv/bin/python scripts/init_db.py --snapshot > init_db.sql
-- 日常建表建议直接跑：
--     app/.venv/bin/python scripts/init_db.py            # 只建缺失的表
--     app/.venv/bin/python scripts/init_db.py --drop     # 开发期重置（清空数据）
--
-- 注意 1：本脚本只 CREATE ... IF NOT EXISTS，不会给已存在的表加列/改类型。
--         改过 models.py 后，开发库请 --drop 重建，正式环境等接 alembic（TODO.md P1）。
-- 注意 2：向量部分还没启用（pgvector 暂不处理）：chunks 表没有 embedding 列；
--         等向量方案定了再补 CREATE EXTENSION vector / vector(N) 列 / HNSW 索引（TODO.md P0-M3）。
--         本机 PG 17 目前没装 pgvector 扩展，直接跑 CREATE EXTENSION 会报错，所以先注释掉。
-- ============================================================

-- CREATE EXTENSION IF NOT EXISTS vector;   -- M3 启用向量化时再打开

-- 需要彻底重置时（会清空全部数据）手工执行下面几行：
-- DROP TABLE IF EXISTS chunks, documents, knowledge_bases, tokens, users CASCADE;

BEGIN;

CREATE TABLE IF NOT EXISTS users (
	id UUID NOT NULL, 
	username VARCHAR(50) NOT NULL, 
	email VARCHAR(255) NOT NULL, 
	hashed_password VARCHAR(255) NOT NULL, 
	role VARCHAR(20) DEFAULT 'user' NOT NULL, 
	is_active BOOLEAN DEFAULT true NOT NULL, 
	last_login_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE, 
	deleted_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	UNIQUE (username), 
	UNIQUE (email)
);

CREATE TABLE IF NOT EXISTS knowledge_bases (
	id UUID NOT NULL, 
	owner_id UUID NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	description TEXT DEFAULT '' NOT NULL, 
	visibility VARCHAR(16) DEFAULT 'private' NOT NULL, 
	chunk_size INTEGER DEFAULT 800 NOT NULL, 
	chunk_overlap INTEGER DEFAULT 120 NOT NULL, 
	embedding_model VARCHAR(100), 
	embedding_dim SMALLINT, 
	document_count INTEGER DEFAULT 0 NOT NULL, 
	chunk_count INTEGER DEFAULT 0 NOT NULL, 
	version INTEGER DEFAULT 1 NOT NULL, 
	last_indexed_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE, 
	deleted_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_kb_owner_name UNIQUE (owner_id, name), 
	FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_kb_owner_updated ON knowledge_bases (owner_id, updated_at);

CREATE TABLE IF NOT EXISTS tokens (
	id UUID NOT NULL, 
	token_hash VARCHAR(64) NOT NULL, 
	user_id UUID NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	client_ip VARCHAR(45), 
	user_agent VARCHAR(256), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (token_hash), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_tokens_user_expires ON tokens (user_id, expires_at);

CREATE INDEX IF NOT EXISTS ix_tokens_expires ON tokens (expires_at);

CREATE TABLE IF NOT EXISTS documents (
	id UUID NOT NULL, 
	kb_id UUID NOT NULL, 
	owner_id UUID NOT NULL, 
	title VARCHAR(255) NOT NULL, 
	filename VARCHAR(255) NOT NULL, 
	file_path VARCHAR(512) NOT NULL, 
	file_size BIGINT DEFAULT 0 NOT NULL, 
	mime_type VARCHAR(128), 
	file_hash VARCHAR(64) NOT NULL, 
	status VARCHAR(16) DEFAULT 'pending' NOT NULL, 
	error_msg TEXT, 
	chunk_count INTEGER DEFAULT 0 NOT NULL, 
	token_count INTEGER DEFAULT 0 NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE, 
	deleted_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_document_kb_file_hash UNIQUE (kb_id, file_hash), 
	FOREIGN KEY(kb_id) REFERENCES knowledge_bases (id) ON DELETE CASCADE, 
	FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_document_kb_created ON documents (kb_id, created_at);

CREATE INDEX IF NOT EXISTS ix_document_owner_status ON documents (owner_id, status);

CREATE TABLE IF NOT EXISTS chunks (
	id UUID NOT NULL, 
	document_id UUID NOT NULL, 
	kb_id UUID NOT NULL, 
	owner_id UUID NOT NULL, 
	seq INTEGER NOT NULL, 
	content TEXT NOT NULL, 
	token_count INTEGER DEFAULT 0 NOT NULL, 
	page INTEGER, 
	heading VARCHAR(255), 
	extra JSONB DEFAULT '{}' NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_chunk_document_seq UNIQUE (document_id, seq), 
	FOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE CASCADE, 
	FOREIGN KEY(kb_id) REFERENCES knowledge_bases (id) ON DELETE CASCADE, 
	FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_chunk_kb ON chunks (kb_id);

CREATE INDEX IF NOT EXISTS ix_chunk_owner_kb ON chunks (owner_id, kb_id);

COMMIT;
