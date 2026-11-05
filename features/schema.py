"""Database tables for phases 3-10. Everything is CREATE ... IF NOT EXISTS, so it is safe to run on every start
and never touches existing data."""
STATEMENTS = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active TIMESTAMPTZ",

    # Phase 3: buddies. user_a is always the smaller id, so a pair can only exist once.
    """CREATE TABLE IF NOT EXISTS friendships (
        id BIGSERIAL PRIMARY KEY,
        user_a BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        user_b BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        requester_id BIGINT NOT NULL, status VARCHAR(10) NOT NULL DEFAULT 'pending',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), responded_at TIMESTAMPTZ, UNIQUE (user_a, user_b))""",
    "CREATE INDEX IF NOT EXISTS friendships_b_idx ON friendships (user_b)",

    # Phase 4: squads
    """CREATE TABLE IF NOT EXISTS squads (
        id BIGSERIAL PRIMARY KEY, name VARCHAR(60) NOT NULL, description VARCHAR(500) NOT NULL DEFAULT '',
        category VARCHAR(30) NOT NULL DEFAULT 'Other', privacy VARCHAR(10) NOT NULL DEFAULT 'public',
        created_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
        avatar BYTEA, avatar_mime VARCHAR(20), avatar_v INT NOT NULL DEFAULT 0,
        archived BOOLEAN NOT NULL DEFAULT FALSE,
        allow_member_invites BOOLEAN NOT NULL DEFAULT FALSE,
        allow_member_quizzes BOOLEAN NOT NULL DEFAULT FALSE,
        allow_member_sessions BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
    """CREATE TABLE IF NOT EXISTS squad_members (
        squad_id BIGINT NOT NULL REFERENCES squads(id) ON DELETE CASCADE,
        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        role VARCHAR(10) NOT NULL DEFAULT 'member', joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (squad_id, user_id))""",
    "CREATE INDEX IF NOT EXISTS squad_members_user_idx ON squad_members (user_id)",
    # kind 'request' = a student asked to join, 'invite' = a squad member invited a student
    """CREATE TABLE IF NOT EXISTS squad_requests (
        id BIGSERIAL PRIMARY KEY, squad_id BIGINT NOT NULL REFERENCES squads(id) ON DELETE CASCADE,
        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind VARCHAR(10) NOT NULL, invited_by BIGINT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (squad_id, user_id))""",

    # Phase 5: messaging (one engine for squad chats and private chats)
    """CREATE TABLE IF NOT EXISTS threads (
        id BIGSERIAL PRIMARY KEY, kind VARCHAR(10) NOT NULL,
        squad_id BIGINT UNIQUE REFERENCES squads(id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
    """CREATE TABLE IF NOT EXISTS dm_pairs (
        user_a BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        user_b BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        thread_id BIGINT NOT NULL REFERENCES threads(id) ON DELETE CASCADE, PRIMARY KEY (user_a, user_b))""",
    """CREATE TABLE IF NOT EXISTS thread_members (
        thread_id BIGINT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        last_delivered_id BIGINT NOT NULL DEFAULT 0, last_read_id BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (thread_id, user_id))""",
    "CREATE INDEX IF NOT EXISTS thread_members_user_idx ON thread_members (user_id)",
    """CREATE TABLE IF NOT EXISTS thread_messages (
        id BIGSERIAL PRIMARY KEY, thread_id BIGINT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        body VARCHAR(2000) NOT NULL DEFAULT '', kind VARCHAR(10) NOT NULL DEFAULT 'text',
        ref_id BIGINT, reply_to BIGINT, deleted BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
    "CREATE INDEX IF NOT EXISTS thread_messages_idx ON thread_messages (thread_id, id)",
    """CREATE TABLE IF NOT EXISTS message_reactions (
        message_id BIGINT NOT NULL REFERENCES thread_messages(id) ON DELETE CASCADE,
        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        emoji VARCHAR(8) NOT NULL, PRIMARY KEY (message_id, user_id, emoji))""",
    """CREATE TABLE IF NOT EXISTS files (
        id BIGSERIAL PRIMARY KEY, owner_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        thread_id BIGINT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
        filename VARCHAR(120) NOT NULL, mime VARCHAR(80) NOT NULL, kind VARCHAR(10) NOT NULL,
        size INT NOT NULL, data BYTEA NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",

    # Phase 6: questions shared with a squad
    """CREATE TABLE IF NOT EXISTS math_questions (
        id BIGSERIAL PRIMARY KEY, squad_id BIGINT NOT NULL REFERENCES squads(id) ON DELETE CASCADE,
        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        body VARCHAR(1500) NOT NULL, category VARCHAR(30) NOT NULL DEFAULT 'Algebra',
        difficulty VARCHAR(10) NOT NULL DEFAULT 'Medium', solution TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
    """CREATE TABLE IF NOT EXISTS question_replies (
        id BIGSERIAL PRIMARY KEY, question_id BIGINT NOT NULL REFERENCES math_questions(id) ON DELETE CASCADE,
        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        body VARCHAR(2000) NOT NULL, is_answer BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
    """CREATE TABLE IF NOT EXISTS reply_helpful (
        reply_id BIGINT NOT NULL REFERENCES question_replies(id) ON DELETE CASCADE,
        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, PRIMARY KEY (reply_id, user_id))""",

    # Phase 7: academic library
    """CREATE TABLE IF NOT EXISTS resources (
        id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title VARCHAR(120) NOT NULL, description VARCHAR(500) NOT NULL DEFAULT '',
        doc_type VARCHAR(30) NOT NULL, university VARCHAR(80) NOT NULL DEFAULT '', course VARCHAR(80) NOT NULL DEFAULT '',
        acad_year VARCHAR(12) NOT NULL DEFAULT '', semester VARCHAR(12) NOT NULL DEFAULT '',
        subject VARCHAR(60) NOT NULL DEFAULT '', filename VARCHAR(120) NOT NULL, mime VARCHAR(80) NOT NULL,
        kind VARCHAR(10) NOT NULL, size INT NOT NULL, data BYTEA NOT NULL, downloads INT NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
    "CREATE INDEX IF NOT EXISTS resources_created_idx ON resources (created_at)",

    # Phase 8: solver history
    """CREATE TABLE IF NOT EXISTS math_solves (
        id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        question VARCHAR(500) NOT NULL, category VARCHAR(30) NOT NULL DEFAULT 'Algebra',
        steps TEXT NOT NULL DEFAULT '', answer VARCHAR(300) NOT NULL DEFAULT '',
        saved BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
    "CREATE INDEX IF NOT EXISTS math_solves_user_idx ON math_solves (user_id, id)",

    # Phase 9: points and quizzes
    """CREATE TABLE IF NOT EXISTS points (
        id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        squad_id BIGINT, kind VARCHAR(20) NOT NULL, amount INT NOT NULL, ref BIGINT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
    "CREATE INDEX IF NOT EXISTS points_user_idx ON points (user_id, created_at)",
    """CREATE TABLE IF NOT EXISTS quizzes (
        id BIGSERIAL PRIMARY KEY, squad_id BIGINT NOT NULL REFERENCES squads(id) ON DELETE CASCADE,
        creator_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title VARCHAR(100) NOT NULL, description VARCHAR(500) NOT NULL DEFAULT '',
        duration_min INT NOT NULL DEFAULT 10, closes_at TIMESTAMPTZ, published BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
    """CREATE TABLE IF NOT EXISTS quiz_questions (
        id BIGSERIAL PRIMARY KEY, quiz_id BIGINT NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
        pos INT NOT NULL, kind VARCHAR(8) NOT NULL, prompt VARCHAR(600) NOT NULL,
        options TEXT NOT NULL DEFAULT '', answer VARCHAR(300) NOT NULL, explanation VARCHAR(600) NOT NULL DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS quiz_attempts (
        id BIGSERIAL PRIMARY KEY, quiz_id BIGINT NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), submitted_at TIMESTAMPTZ,
        score INT NOT NULL DEFAULT 0, max_score INT NOT NULL DEFAULT 0, late BOOLEAN NOT NULL DEFAULT FALSE,
        UNIQUE (quiz_id, user_id))""",
    """CREATE TABLE IF NOT EXISTS quiz_answers (
        attempt_id BIGINT NOT NULL REFERENCES quiz_attempts(id) ON DELETE CASCADE,
        question_id BIGINT NOT NULL REFERENCES quiz_questions(id) ON DELETE CASCADE,
        answer VARCHAR(400) NOT NULL DEFAULT '', correct BOOLEAN NOT NULL DEFAULT FALSE,
        PRIMARY KEY (attempt_id, question_id))""",

    # Phase 10: study sessions
    """CREATE TABLE IF NOT EXISTS study_sessions (
        id BIGSERIAL PRIMARY KEY, squad_id BIGINT NOT NULL REFERENCES squads(id) ON DELETE CASCADE,
        creator_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        topic VARCHAR(100) NOT NULL, description VARCHAR(500) NOT NULL DEFAULT '',
        starts_at TIMESTAMPTZ NOT NULL, duration_min INT NOT NULL DEFAULT 60,
        location VARCHAR(150) NOT NULL DEFAULT '', cancelled BOOLEAN NOT NULL DEFAULT FALSE,
        reminded BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
    "CREATE INDEX IF NOT EXISTS study_sessions_start_idx ON study_sessions (starts_at)",
    """CREATE TABLE IF NOT EXISTS session_rsvps (
        session_id BIGINT NOT NULL REFERENCES study_sessions(id) ON DELETE CASCADE,
        user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        status VARCHAR(8) NOT NULL, PRIMARY KEY (session_id, user_id))""",
]
