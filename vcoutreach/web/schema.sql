-- Empty workspace schema. Legacy tables support retained review and integrations.

CREATE TABLE IF NOT EXISTS batches(id INTEGER PRIMARY KEY, name TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);

CREATE TABLE IF NOT EXISTS contact_batches(contact_id INTEGER REFERENCES partners(id) ON DELETE CASCADE, batch_id INTEGER REFERENCES batches(id), PRIMARY KEY(contact_id,batch_id));

CREATE TABLE IF NOT EXISTS contact_provider_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_id INTEGER NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('apollo', 'enrow')),
    operation TEXT NOT NULL CHECK (operation IN ('email_find', 'email_verify', 'person_enrich')),
    state TEXT NOT NULL,
    candidate_email TEXT COLLATE NOCASE,
    candidate_email_hash TEXT,
    provider_email_status TEXT,
    confidence REAL,
    http_status INTEGER,
    request_count INTEGER NOT NULL DEFAULT 1,
    retry_count INTEGER NOT NULL DEFAULT 0,
    provider_cost_microunits INTEGER NOT NULL DEFAULT 0,
    candidate_disposition TEXT NOT NULL DEFAULT 'none'
        CHECK (candidate_disposition IN ('none', 'review_required', 'same_as_current', 'applied', 'dismissed')),
    send_gate_effect TEXT NOT NULL DEFAULT 'none'
        CHECK (send_gate_effect IN ('none', 'eligible', 'blocked')),
    evidence_json TEXT NOT NULL DEFAULT '{}',
    idempotency_key TEXT NOT NULL UNIQUE,
    observed_at TEXT NOT NULL,
    applied_at TEXT,
    dismissed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS contact_provider_connections (
    provider TEXT PRIMARY KEY CHECK (provider IN ('apollo', 'enrow')),
    health_status TEXT NOT NULL,
    credits_remaining REAL,
    http_status INTEGER,
    last_error_code TEXT,
    checked_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS crm_activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_id INTEGER NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    activity_type TEXT NOT NULL,
    subject TEXT,
    snippet TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS crm_campaign_qualifications (
    partner_id INTEGER NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    campaign_key TEXT NOT NULL,
    qualification_recommendation TEXT NOT NULL,
    fit_review TEXT,
    check_size_status TEXT,
    source_path TEXT NOT NULL,
    source_url TEXT,
    supporting_sources_json TEXT NOT NULL DEFAULT '[]',
    pitchbook_id TEXT,
    observed_at TEXT,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (partner_id, campaign_key)
);

CREATE TABLE IF NOT EXISTS crm_firms (
    firm_key TEXT PRIMARY KEY,
    firm_name TEXT NOT NULL,
    check_size_min INTEGER,
    check_size_max INTEGER,
    fund_status TEXT,
    evidence_note TEXT,
    pitchbook_url TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS crm_mail_evidence (
    message_id TEXT PRIMARY KEY,
    partner_id INTEGER NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    thread_id TEXT,
    source_mailbox TEXT NOT NULL,
    evidence_kind TEXT NOT NULL DEFAULT 'incoming_mail',
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS crm_outreach_queues (
    campaign_key TEXT NOT NULL,
    lane TEXT NOT NULL CHECK (lane IN ('followup_watchlist', 'followup_next', 'new_research')),
    partner_id INTEGER NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    queue_rank INTEGER NOT NULL,
    readiness TEXT NOT NULL CHECK (readiness IN ('review_ready', 'copy_needed', 'research_needed', 'waiting')), 
    rationale TEXT NOT NULL,
    evidence_source TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, candidate_email TEXT,
    PRIMARY KEY (campaign_key, lane, partner_id),
    UNIQUE (campaign_key, lane, queue_rank)
);

CREATE TABLE IF NOT EXISTS crm_profiles (
    partner_id INTEGER PRIMARY KEY REFERENCES partners(id) ON DELETE CASCADE,
    stage TEXT NOT NULL DEFAULT 'prospect',
    fit_grade TEXT NOT NULL DEFAULT 'unreviewed',
    fit_rationale TEXT,
    next_follow_up_at TEXT,
    follow_up_reason TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
, on_update_list INTEGER NOT NULL DEFAULT 0, fit_rank INTEGER);

CREATE TABLE IF NOT EXISTS crm_send_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_id INTEGER NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    batch_key TEXT NOT NULL,
    evidence_status TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source_path TEXT NOT NULL,
    UNIQUE(partner_id, batch_key)
);

CREATE TABLE IF NOT EXISTS crm_sync_state (
    source TEXT PRIMARY KEY,
    checked_at TEXT NOT NULL,
    scanned_messages INTEGER NOT NULL,
    matched_messages INTEGER NOT NULL,
    scan_complete INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS crm_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_id INTEGER NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    due_at TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS crm_tracking_actions (
    action_id TEXT PRIMARY KEY,
    partner_id INTEGER NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    batch_key TEXT,
    activated_at TEXT,
    last_synced_at TEXT,
    sync_status TEXT
);

CREATE TABLE IF NOT EXISTS crm_tracking_events (
    action_id TEXT NOT NULL,
    partner_id INTEGER NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    occurred_at TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('open', 'click', 'deck_view')),
    source_kind TEXT NOT NULL,
    user_agent_family TEXT,
    network_class TEXT,
    classification TEXT NOT NULL CHECK (classification IN ('automation_likely', 'unknown', 'human_likely')),
    reason_codes_json TEXT NOT NULL DEFAULT '[]',
    seconds_since_send INTEGER,
    synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (action_id, occurred_at, event_type, source_kind)
);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_id INTEGER NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    hook TEXT NOT NULL,          
    subject TEXT NOT NULL,       
    boilerplate_version TEXT NOT NULL,  
    status TEXT NOT NULL DEFAULT 'pending',  
    sender_account TEXT DEFAULT 'sender',  
    review_notes TEXT,           
    gmail_message_id TEXT,       
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    approved_at TEXT,
    drafted_at TEXT,
    sent_at TEXT, sent_to_address TEXT, live_sent_at TEXT, live_sent_to TEXT, live_message_id TEXT, live_subject TEXT, test_sent_count INTEGER DEFAULT 0, last_test_sent_at TEXT, hallucination_check TEXT, auto_approve_skip_reason TEXT, thread_id TEXT, in_reply_to_message_id TEXT, is_reply INTEGER DEFAULT 0,
    UNIQUE(partner_id, boilerplate_version)
);

CREATE TABLE IF NOT EXISTS enrichment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_id INTEGER NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    source TEXT NOT NULL,        
    url TEXT NOT NULL DEFAULT '', 
    raw_text TEXT,               
    facts_json TEXT,             
    confidence REAL,             
    fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(partner_id, source, url)
);

CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS partners (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_name TEXT NOT NULL,
    first_name TEXT,             
    firm TEXT,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    tier INTEGER,                
    responsiveness TEXT,         
    channel TEXT DEFAULT 'email',
    email_uncertain INTEGER DEFAULT 0,  
    notes TEXT,                  
    source TEXT DEFAULT 'import',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
, linkedin_url TEXT, firm_domain TEXT, twitter_handle TEXT, enriched_at TEXT, pitchbook_id TEXT, pitchbook_firm_id TEXT, pitchbook_last_scraped_at TEXT, dropped_reason TEXT, verification_status TEXT DEFAULT 'unverified', email_verified_at TEXT, email_verification_status TEXT, email_verification_confidence REAL, email_verification_method TEXT, needs_enrichment INTEGER DEFAULT 0, emailable_state TEXT, emailable_score INTEGER, emailable_reason TEXT, emailable_did_you_mean TEXT, emailable_checked_at TIMESTAMP, rescue_attempted_at TIMESTAMP, rescue_source TEXT, rescue_confidence REAL, pb_email TEXT, pb_email_source TEXT, pb_linkedin TEXT, dedup_of INTEGER REFERENCES partners(id), audience TEXT DEFAULT 'vc', target_role TEXT, company TEXT, campaign TEXT, slug TEXT, role TEXT DEFAULT "", facts TEXT DEFAULT "[]", source_urls TEXT DEFAULT "[]", lane TEXT DEFAULT "first_touch", thread_subject TEXT DEFAULT "", in_reply_to TEXT DEFAULT "", refs TEXT DEFAULT "", tags TEXT DEFAULT "", next_action TEXT DEFAULT "", updated_at TEXT);

CREATE TABLE IF NOT EXISTS pitchbook_usage (
    date TEXT PRIMARY KEY,
    lookups INTEGER DEFAULT 0,
    last_lookup_at TEXT
);

CREATE TABLE IF NOT EXISTS replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_id INTEGER REFERENCES partners(id) ON DELETE SET NULL,
    draft_id INTEGER REFERENCES drafts(id) ON DELETE SET NULL,
    gmail_thread_id TEXT NOT NULL,
    gmail_message_id TEXT NOT NULL UNIQUE,
    from_email TEXT,
    from_name TEXT,
    subject TEXT,
    snippet TEXT,
    body_text TEXT,
    received_at TEXT,
    label TEXT,
    classification_confidence REAL,
    classification_summary TEXT,
    suggested_action TEXT,
    classified_at TEXT,
    user_reviewed INTEGER DEFAULT 0,
    user_label TEXT,
    discovered_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS scheduled_sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_ids TEXT NOT NULL,
    mode TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    estimated_completion TEXT,
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    completed_at TEXT,
    result_json TEXT,
    created_by TEXT DEFAULT 'web',
    confirmation TEXT
);

CREATE TABLE IF NOT EXISTS schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP);

CREATE TABLE IF NOT EXISTS web_search_cache (
    query TEXT PRIMARY KEY,      
    results_json TEXT NOT NULL,
    cached_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS web_send_journal(id INTEGER PRIMARY KEY, draft_id INTEGER REFERENCES drafts(id) ON DELETE SET NULL, partner_id INTEGER REFERENCES partners(id) ON DELETE SET NULL, recipient TEXT NOT NULL, lane TEXT NOT NULL, mode TEXT NOT NULL, state TEXT NOT NULL, target_email TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP, completed_at TEXT, message_id TEXT);

CREATE INDEX IF NOT EXISTS idx_contact_provider_attempts_candidate_hash
    ON contact_provider_attempts(candidate_email_hash);

CREATE INDEX IF NOT EXISTS idx_contact_provider_attempts_partner
    ON contact_provider_attempts(partner_id, observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_contact_provider_attempts_provider_state
    ON contact_provider_attempts(provider, state, observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_crm_activities_partner ON crm_activities(partner_id, created_at);

CREATE INDEX IF NOT EXISTS idx_crm_campaign_qualification_campaign
ON crm_campaign_qualifications(campaign_key, partner_id);

CREATE INDEX IF NOT EXISTS idx_crm_mail_evidence_partner
ON crm_mail_evidence(partner_id, observed_at);

CREATE INDEX IF NOT EXISTS idx_crm_outreach_queues_lane
ON crm_outreach_queues(campaign_key, lane, queue_rank);

CREATE INDEX IF NOT EXISTS idx_crm_profiles_fit_rank
ON crm_profiles(fit_grade, fit_rank);

CREATE INDEX IF NOT EXISTS idx_crm_profiles_follow_up ON crm_profiles(next_follow_up_at);

CREATE INDEX IF NOT EXISTS idx_crm_profiles_stage ON crm_profiles(stage);

CREATE INDEX IF NOT EXISTS idx_crm_profiles_update_list
ON crm_profiles(on_update_list);

CREATE INDEX IF NOT EXISTS idx_crm_send_evidence_partner ON crm_send_evidence(partner_id, observed_at);

CREATE INDEX IF NOT EXISTS idx_crm_tasks_due ON crm_tasks(status, due_at);

CREATE INDEX IF NOT EXISTS idx_crm_tasks_partner ON crm_tasks(partner_id, status);

CREATE INDEX IF NOT EXISTS idx_crm_tracking_actions_partner
ON crm_tracking_actions(partner_id);

CREATE INDEX IF NOT EXISTS idx_crm_tracking_events_partner
ON crm_tracking_events(partner_id, occurred_at);

CREATE INDEX IF NOT EXISTS idx_drafts_partner ON drafts(partner_id);

CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);

CREATE INDEX IF NOT EXISTS idx_enrichment_partner ON enrichment(partner_id);

CREATE INDEX IF NOT EXISTS idx_replies_label ON replies(label);

CREATE INDEX IF NOT EXISTS idx_replies_partner ON replies(partner_id);

CREATE INDEX IF NOT EXISTS idx_replies_received ON replies(received_at)
;

CREATE INDEX IF NOT EXISTS idx_scheduled_status ON scheduled_sends(status, scheduled_for);

CREATE UNIQUE INDEX IF NOT EXISTS partners_slug ON partners(slug);

CREATE UNIQUE INDEX IF NOT EXISTS web_live_once ON web_send_journal(recipient,lane) WHERE mode='live' AND state IN ('sending','sent','uncertain');

CREATE TRIGGER IF NOT EXISTS drafts_updated_at
AFTER UPDATE ON drafts
FOR EACH ROW
BEGIN
    UPDATE drafts SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;
