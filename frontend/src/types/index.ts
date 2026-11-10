export interface User {
  id: string;
  email: string;
  name: string;
  is_admin?: boolean;
  groups?: string[];
  role?: string;
  avatar?: string;
  dateOfBirth?: string;
  profileCompleted?: boolean;
  created_at?: string;
  /** Tokens left of the allowance (individual accounts). */
  token_balance?: number;
  /** Language answers are written in (Settings); `en` by default. */
  preferred_language?: string;
  /** Language questions are written in; empty/null means the same as the answer language. */
  question_language?: string | null;
  /** When the token wallet is next refilled (ISO, UTC). */
  token_refill_at?: string | null;
  /** `individual` (default) or `organisation`; older servers omit it. */
  account_type?: AccountType;
  organization_id?: string | null;
  organization_name?: string | null;
  org_role?: OrgRole | null;
  is_active?: boolean;
  must_change_password?: boolean;
}

export type AccountType = 'individual' | 'organisation';
export type OrgRole = 'owner' | 'admin' | 'member';
export type OrganisationStatus =
  | 'requested'
  | 'pending_activation'
  | 'active'
  | 'suspended'
  | 'deactivated'
  | 'rejected';

/** Response of every endpoint that signs a user in (`Token` on the backend). */
export interface AuthTokenResponse {
  access_token: string;
  token_type?: string;
  user: User;
}

export interface Citation {
  id?: string;
  chunk_id?: string;
  source_type?: string;
  document: string;
  page: number;
  section?: string;
  score: number;
  snippet: string;
}

export interface WebSource {
  id?: string;
  title: string;
  url: string;
  domain: string;
  snippet: string;
}

export interface WebImage {
  url: string;
  description?: string | null;
  alt_text?: string | null;
  source_url?: string | null;
  source_title?: string | null;
  relevance_score?: number | null;
}

export interface DocumentAttachment {
  name: string;
  file_type?: string;
  size_bytes?: number;
  page_count?: number;
  text?: string;
  preview?: string;
}

export type MessageFeedback = 'up' | 'down';
export type ChatMode = 'sprint' | 'scholar';
export const DEFAULT_CHAT_MODE: ChatMode = 'scholar';

/** Token estimates for one answer: what Clyptus.AI used, what a typical AI chatbot would, and the difference. */
export interface TokenUsage {
  used: number;
  baseline: number;
  saved: number;
  /** Free tokens this answer cost the user (0 to 1). */
  cost?: number;
}

/** GET /auth/me/token-savings: the stored figures of all of the user's answers, summed. */
export interface TokenSavingsSummary {
  answers: number;
  used: number;
  baseline: number;
  saved: number;
}

export interface Message {
  id: string;
  chat_id: string;
  role: 'user' | 'assistant';
  content: string;
  attachments?: DocumentAttachment[];
  source_type?: 'knowledge_base' | 'github_repo' | 'web' | 'combined' | 'abstention' | 'insufficient' | 'refusal' | 'error' | 'interrupted' | 'security_policy' | 'conversational' | 'attached_document' | 'unavailable' | 'sprint';
  grounding_score?: number | null;
  confidence?: number;
  grounding?: string;
  verification_status?: 'verified' | 'partial' | 'unverified' | 'abstention' | 'restricted' | 'unavailable';
  internal_evidence_confidence?: number;
  selected_evidence_quality?: number;
  kb_score?: number;
  web_score?: number;
  winning_score?: number;
  source_scores?: Record<string, number>;
  is_in_rag_pipeline?: boolean;
  external_search_used?: boolean;
  follow_up_questions?: string[];
  /** Only on answers streamed in this session; not stored with the message. */
  token_usage?: TokenUsage;
  feedback?: MessageFeedback | null;
  security_restricted?: boolean;
  violation_category?: string;
  llm_provider?: string;
  llm_model?: string;
  regeneration_count?: number;
  created_at: string;
  citations: Citation[];
  web_sources: WebSource[];
  images?: WebImage[];
}

export interface Chat {
  id: string;
  user_id?: string;
  /** The project the chat was started in, if any. */
  project_id?: string | null;
  title: string;
  created_at: string;
  updated_at: string;
  isPinned?: boolean;
  message_count?: number;
}

export interface Project {
  id: string;
  name: string;
  description?: string;
  createdAt: string;
  updatedAt: string;
  isPinned: boolean;
  /** Chats saved in this project. */
  chatCount?: number;
}

export interface KBDocument {
  id: string;
  filename: string;
  title: string;
  file_type: string;
  file_size_bytes: number;
  file_size_formatted: string;
  page_count: number;
  chunk_count: number;
  status: 'pending' | 'processing' | 'completed' | 'failed';
  stage: string;
  error_message?: string | null;
  vector_indexed: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface DocumentCounts {
  all: number;
  pending: number;
  processing: number;
  completed: number;
  failed: number;
  total_chunks: number;
}

export interface ProcessingProgress {
  is_processing: boolean;
  total_selected: number;
  completed_count: number;
  processing_count: number;
  pending_count: number;
  failed_count: number;
  total_chunks_generated: number;
  total_embeddings_stored: number;
  overall_progress_pct: number;
  current_document_id?: string | null;
  current_document_name?: string | null;
  current_stage: string;
  verification_status: 'Pending' | 'Successful' | 'Partial' | 'Failed';
  summary?: {
    documents_processed: number;
    completed_documents?: number;
    chunks_generated: number;
    embeddings_stored: number;
    failed_documents: number;
    vector_database_verification: string;
  } | null;
  recent_logs?: string[];
  timestamp?: string;
}

export interface DocumentListResponse {
  documents: KBDocument[];
  total: number;
  page: number;
  page_size: number;
  counts: DocumentCounts;
}


// ==========================================
// Organisation Portal (docs/ORGANISATION_PORTAL.md)
// ==========================================

export interface OrgRequestPayload {
  organisation_name: string;
  contact_name: string;
  contact_email: string;
  message?: string;
}

export interface ActivationCodeDetails {
  organisation_name: string;
  admin_email: string;
  admin_name: string;
  expires_at: string;
}

export interface OrgSummary {
  id: string;
  name: string;
  status: OrganisationStatus;
  member_count: number;
  created_at: string;
  /** Returned by the backend in addition to the documented fields. */
  contact_email?: string;
}

export interface OrgMe {
  organisation: OrgSummary;
  role: OrgRole;
  can_manage: boolean;
}

export interface KnowledgeCategory {
  key: string;
  label: string;
  count: number;
}

export interface SharedAnswerSummary {
  id: string;
  question: string;
  answer_preview: string;
  category: string;
  category_label: string;
  shared_by_name: string;
  created_at: string;
  like_count: number;
  comment_count: number;
  liked_by_me: boolean;
}

export interface SharedAnswerLiker {
  name: string;
  liked_at: string;
}

export interface SharedAnswerComment {
  id: string;
  author_name: string;
  body: string;
  created_at: string;
  can_delete: boolean;
}

export interface SharedAnswerDetail extends SharedAnswerSummary {
  answer: string;
  likers: SharedAnswerLiker[];
  comments: SharedAnswerComment[];
  can_edit: boolean;
}

export interface KnowledgeListResponse {
  total: number;
  items: SharedAnswerSummary[];
}

export interface OrgMember {
  id: string;
  name: string;
  email: string;
  org_role: OrgRole;
  is_active: boolean;
  must_change_password: boolean;
  created_at: string;
  last_login_at: string | null;
}

export interface OrgMemberCreated {
  member: OrgMember;
}

export interface OrgActivityMember {
  id: string;
  name: string;
  chats: number;
  questions: number;
  shared_answers: number;
  comments: number;
  likes: number;
  last_login_at: string | null;
}

export interface OrgActivity {
  members: OrgActivityMember[];
  totals: Partial<
    Record<'members' | 'active_members' | 'chats' | 'questions' | 'shared_answers' | 'comments' | 'likes', number>
  > &
    Record<string, number>;
}

export interface AdminOrganisation {
  id: string;
  name: string;
  status: OrganisationStatus;
  contact_name: string | null;
  contact_email: string;
  member_count: number;
  created_at: string;
  activated_at: string | null;
  request_message: string | null;
  active_code: { hint: string; expires_at: string; admin_email: string } | null;
}

export interface IssuedActivationCode {
  activation_code: string;
  expires_at: string;
}

export interface AdminOrganisationIssued extends IssuedActivationCode {
  organisation: AdminOrganisation;
}

export interface AdminOrganisationDetail {
  organisation: AdminOrganisation;
  admins: Array<{ name: string; email: string; org_role: OrgRole; is_active: boolean; last_login_at: string | null }>;
  usage: {
    members: number;
    active_members: number;
    questions_30d: number;
    shared_answers: number;
    comments: number;
  };
}

export interface IndividualUser {
  id: string;
  name: string;
  email: string;
  is_admin: boolean;
  is_active: boolean;
  created_at: string;
  last_login_at: string | null;
  chats: number;
  questions: number;
}

export interface IndividualUsersResponse {
  total: number;
  users: IndividualUser[];
}
