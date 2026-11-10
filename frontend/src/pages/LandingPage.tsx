import React from 'react';
import { motion } from 'framer-motion';
import {
  ArrowDown,
  ArrowRight,
  ArrowUpRight,
  BookOpen,
  BookOpenCheck,
  Boxes,
  Compass,
  FileText,
  Gauge,
  Route,
  Search,
  ShieldCheck,
  Sparkles,
  Workflow,
  Zap,
} from 'lucide-react';
import { Link } from 'react-router-dom';
import { FaqSection } from '../components/landing/FaqSection';
import { VideoShowcase } from '../components/landing/VideoShowcase';
import { Footer } from '../components/footer/Footer';
import { PublicNav } from '../components/navbar/PublicNav';
import { PageMeta } from '../components/ui/PageMeta';
import iconOnly from '../assets/clyptusap-icon.png';

const brimAreas = [
  { abbreviation: 'SOM', title: 'Subscription Order Management', icon: Boxes },
  { abbreviation: 'CC', title: 'Convergent Charging', icon: Gauge },
  { abbreviation: 'CI', title: 'Convergent Invoicing', icon: FileText },
  { abbreviation: 'FI-CA', title: 'Contract Accounts Receivable and Payable', icon: BookOpen },
  { abbreviation: 'CM', title: 'Convergent Mediation', icon: Workflow },
  { abbreviation: 'SAP', title: 'S/4HANA and the wider SAP landscape around BRIM', icon: Compass },
];

// The RAG Pipeline, in the order the backend runs it.
const pipelineSteps: Array<[string, string, React.ReactNode, React.ComponentType<{ size?: number }>]> = [
  ['01', 'Route', <><strong>Query routing</strong> and an SAP scope check; follow-ups are resolved against the conversation.</>, Route],
  ['02', 'Retrieve', <><strong>Hybrid search</strong>: dense vector embeddings, BM25 keyword ranking and exact SAP identifier matching.</>, Search],
  ['03', 'Rank', <><strong>Reciprocal rank fusion</strong> and multi-signal reranking select the strongest passages.</>, Gauge],
  ['04', 'Generate', <>The LLM drafts an answer <strong>only from the selected evidence</strong>.</>, Sparkles],
  ['05', 'Verify', <><strong>Claim-level grounding checks</strong> test every statement and SAP identifier against that evidence.</>, ShieldCheck],
  ['06', 'Respond', <>The <strong>verified answer</strong> is streamed to the chat with suggested follow-up questions.</>, ArrowRight],
];

const modes = [
  {
    id: 'sprint',
    name: 'Direct LLM',
    tagline: 'Fast, direct answers on demand',
    icon: Zap,
    points: [
      'Answers straight from the language model, streamed as it is written',
      'Best for definitions, quick how-tos and everyday SAP questions',
      'Remembers the conversation: recent messages, older-turn summary and earlier questions',
      'Reads uploaded PDF, Word, text and Markdown files',
      'Say “save it” or “remember that …” to keep facts for all of your chats',
    ],
  },
  {
    id: 'scholar',
    name: 'RAG Pipeline',
    tagline: 'Researched, verified answers (the default)',
    icon: BookOpenCheck,
    points: [
      'Retrieves from an administrator-curated SAP knowledge base before answering',
      'Falls back to approved SAP sources such as SAP Help Portal and SAP Community when internal evidence is weak',
      'Checks each claim, transaction code and table name against the retrieved evidence',
      'Declines to answer when the evidence does not support one',
    ],
  },
];

const useCases: Array<[string, React.ReactNode]> = [
  ['Configuration', <>Where is this setting maintained in <strong>SPRO</strong> (Customizing)?</>],
  ['Transaction codes', <>Which <strong>transaction</strong> monitors billable items or invoicing runs?</>],
  ['Concepts', <>What is the <strong>lifecycle of a billable item</strong> in Convergent Invoicing?</>],
  ['Processes', <>Walk me through the <strong>SOM → CC → CI → FI-CA</strong> order-to-cash flow.</>],
  ['Troubleshooting', <>The billing run finished but <strong>no invoicing document was created</strong>. What should I check?</>],
  ['Documents', <>Summarise the <strong>FI-CA clearing rules</strong> in the specification I attached.</>],
];

const exampleQuestions = [
  'How does a provider contract in SOM relate to Convergent Charging?',
  'Where are document types configured in FI-CA?',
  'How does Convergent Charging use charge plans to rate usage?',
  'Explain the end-to-end BRIM billing and invoicing flow.',
  'Why was my billing document not generated?',
  'Which transaction monitors billable items in Convergent Invoicing?',
];

function ResponsePreview() {
  return (
    <article className="response-preview">
      <div className="response-preview-top">
        <span><span className="live-dot" /> HIGH MODE</span>
        <span>Verified before display</span>
      </div>
      <div className="response-preview-question">
        <span>QUESTION</span>
        <p>Where are document types for invoicing documents configured in FI-CA?</p>
      </div>
      <div className="response-preview-answer">
        <span>ANSWER</span>
        <p>
          The answer is drafted from passages retrieved for this question, then each statement and SAP
          identifier is checked against those passages before you see it.
        </p>
      </div>
      <div className="response-preview-meta">
        <div><strong>Retrieval</strong><span>Dense + BM25 + identifier match</span></div>
        <div><strong>Verification</strong><span>Claim-level grounding</span></div>
        <div><strong>Scope</strong><span>SAP and SAP BRIM only</span></div>
      </div>
    </article>
  );
}

export const LandingPage: React.FC = () => {
  return (
    <>
      <PageMeta
        title="SAP BRIM AI Knowledge Assistant"
        description="Clyptus.AI answers SAP and SAP BRIM questions with hybrid retrieval and claim-level verification."
      />
      <PublicNav />
      <main>
        {/* Hero Section */}
        <section className="hero section-wrap">
          <motion.div
            className="hero-copy"
            initial={{ opacity: 0, y: 18 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.55 }}
          >
            <div className="hero-kicker">
              <span className="kicker-brand-mark">
                <img src={iconOnly} alt="" className="h-4 w-4 object-contain" />
              </span>
              <strong>SAP BRIM</strong> KNOWLEDGE ASSISTANT
            </div>
            <h1>
              Your SAP questions.<br />
              <span>Answered with</span><br />
              exact context.
            </h1>
            <p className="hero-description">
              <strong>Clyptus.AI</strong> helps SAP consultants and architects work through <strong>SAP BRIM</strong>:
              researched answers in the <strong>RAG Pipeline</strong> by default, or quick answers when you switch to
              <strong>Direct LLM</strong>, using the configured language model directly. RAG answers are <strong>verified claim by claim</strong>.
            </p>
            <div className="hero-actions">
              <Link className="button button-brand" to="/login">
                Ask Clyptus.AI <ArrowUpRight size={17} />
              </Link>
              <Link className="text-link" to="/how-it-works">
                See how it works <ArrowRight size={16} />
              </Link>
            </div>
            <div className="hero-trust">
              <span className="trust-line" />
              <strong>SAP-only scope</strong> · <strong>Hybrid retrieval</strong> · <strong>Claim-level verification</strong>
            </div>
          </motion.div>

          <motion.div
            className="hero-visual"
            initial={{ opacity: 0, y: 22 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.7, delay: 0.14 }}
          >
            <VideoShowcase />
            <div className="video-supporting-copy">
              <strong>See Clyptus.AI in action</strong>
              <p>
                Ask in plain language. The <strong>RAG Pipeline</strong> retrieves relevant SAP knowledge, checks the draft
                against it, and streams the <strong>verified answer</strong>.
              </p>
            </div>
          </motion.div>
          <a className="scroll-hint" href="#why">
            <span>SCROLL TO EXPLORE</span>
            <ArrowDown size={14} />
          </a>
        </section>

        {/* Statement Band */}
        <section className="statement-band" id="why">
          <div className="statement-inner">
            <span className="eyebrow">SAP KNOWLEDGE, IN CONTEXT</span>
            <p>
              From complex SAP configuration questions<br />
              <span>to <strong>evidence-backed answers</strong> you can act on.</span>
            </p>
          </div>
        </section>

        {/* Intro Section */}
        <section className="section section-wrap intro-section">
          <div className="intro-label">
            <span className="eyebrow">WHAT IS CLYPTUS.AI</span>
          </div>
          <div className="intro-content">
            <h2>An AI assistant<br />built specifically for <span>SAP knowledge.</span></h2>
            <p>
              SAP answers depend on <strong>module integration</strong>, <strong>transaction codes</strong>,
              <strong> Customizing (SPRO) settings</strong> and <strong>release-specific behaviour</strong>. A generic chatbot
              guesses at these; Clyptus.AI looks them up.
            </p>
            <p>
              The <strong>RAG Pipeline</strong> finds the relevant documentation
              before the model answers, and a <strong>grounding check</strong> rejects statements the evidence does not
              support. That greatly reduces invented settings and transaction codes.
            </p>
            <Link className="text-link" to="/how-it-works">
              See how answers are verified <ArrowRight size={16} />
            </Link>
          </div>
        </section>

        {/* Modes Section */}
        <section className="section section-wrap modes-section" id="modes">
          <div className="features-heading">
            <div>
              <span className="eyebrow">ONE BUTTON, TWO WAYS TO ASK</span>
              <h2>RAG Pipeline for depth.<br /><span>Direct LLM for speed.</span></h2>
            </div>
            <p>
              Every chat starts with the <strong>RAG Pipeline</strong>. Press <strong>High</strong> in the chat box to switch to
              a direct model response. <strong>Both stay within SAP topics</strong> and
              remember what you discussed earlier in the conversation.
            </p>
          </div>
          <div className="feature-grid feature-grid-two">
            {modes.map(({ id, name, tagline, icon: Icon, points }, index) => (
              <motion.article
                className="feature-card mode-card"
                key={id}
                initial={{ opacity: 0, y: 14 }}
                whileInView={{ opacity: 1, y: 0 }}
                viewport={{ once: true, amount: 0.25 }}
                transition={{ delay: index * 0.06 }}
              >
                <span className="feature-icon"><Icon size={19} strokeWidth={1.7} /></span>
                <span className="brim-abbreviation">{name}</span>
                <h3>{tagline}</h3>
                <ul className="mode-points">
                  {points.map((point) => <li key={point}>{point}</li>)}
                </ul>
              </motion.article>
            ))}
          </div>
        </section>

        {/* Workflow Section */}
        <section className="workflow-section">
          <div className="section section-wrap">
            <div className="workflow-heading">
              <div>
                <span className="eyebrow">THE HIGH-MODE PIPELINE</span>
                <h2>Retrieve first.<br /><span>Verify before you see it.</span></h2>
              </div>
              <p>
                <strong>Dense embeddings</strong>, <strong>BM25</strong> and <strong>SAP identifier matching</strong>,
                fused with <strong>reciprocal rank fusion</strong>, then checked by <strong>claim-level grounding</strong>.
              </p>
            </div>
            <div className="workflow-track workflow-track-six">
              {pipelineSteps.map(([number, title, text, Icon], index) => (
                <motion.div
                  className="workflow-step"
                  key={number}
                  initial={{ opacity: 0, y: 14 }}
                  whileInView={{ opacity: 1, y: 0 }}
                  viewport={{ once: true, amount: 0.35 }}
                  transition={{ delay: index * 0.06 }}
                >
                  <div className="workflow-node"><Icon size={18} /></div>
                  <span className="step-number">{number}</span>
                  <h3>{title}</h3>
                  <p>{text}</p>
                  {index < pipelineSteps.length - 1 && <span className="workflow-connector" aria-hidden="true" />}
                </motion.div>
              ))}
            </div>
          </div>
        </section>

        {/* BRIM Section */}
        <section className="section section-wrap brim-section">
          <div className="features-heading">
            <div>
              <span className="eyebrow">SAP BRIM</span>
              <h2>Built for SAP.<br /><span>Focused on BRIM.</span></h2>
            </div>
            <p>
              Billing and Revenue Innovation Management end to end: <strong>subscription orders</strong>,
              <strong> charging and rating</strong>, <strong>invoicing</strong> and <strong>contract accounting</strong>.
            </p>
          </div>
          <div className="feature-grid">
            {brimAreas.map(({ abbreviation, title, icon: Icon }, index) => (
              <motion.article
                className="feature-card brim-card"
                key={abbreviation}
                initial={{ opacity: 0, y: 14 }}
                whileInView={{ opacity: 1, y: 0 }}
                viewport={{ once: true, amount: 0.25 }}
                transition={{ delay: index * 0.05 }}
              >
                <span className="feature-icon"><Icon size={19} strokeWidth={1.7} /></span>
                <span className="brim-abbreviation">{abbreviation}</span>
                <h3>{title}</h3>
                <span className="feature-card-line" />
              </motion.article>
            ))}
          </div>
        </section>

        {/* Response Preview */}
        <section className="response-section section section-wrap">
          <div className="response-copy">
            <span className="eyebrow">VERIFIED BEFORE YOU SEE IT</span>
            <h2>Don't just get an answer.<br /><span>Get one that was checked.</span></h2>
            <p>
              The RAG Pipeline holds the draft back until <strong>grounding verification</strong> has checked it. Unsupported claims are
              repaired or removed first, and anything that still cannot be confirmed is labelled. Source documents are <strong>curated and access-controlled by your
              administrators</strong>, and they are never exposed in the chat.
            </p>
          </div>
          <ResponsePreview />
        </section>

        {/* Boundary Section */}
        <section className="boundary-section">
          <div className="section section-wrap boundary-grid">
            <article className="boundary-card">
              <span className="eyebrow">WHEN THE EVIDENCE ISN'T THERE</span>
              <h2>No verified answer?<br /><span>Clyptus.AI says so.</span></h2>
              <p>
                If the retrieved documentation does not support a claim, the RAG Pipeline <strong>removes it</strong> instead of
                inventing settings, tables or transaction codes, and tells you what it could not confirm.
              </p>
              <div className="boundary-example">
                <strong>User</strong>
                <p>“What is posting area 9999 used for in FI-CA?”</p>
                <strong>Assistant</strong>
                <p>“The available SAP documentation does not describe a posting area 9999, so I can't confirm what it is used for.”</p>
              </div>
              <div className="boundary-tags">
                <span>Claim-level verification</span>
                <span>SAP identifier validation</span>
                <span>Unconfirmed claims labelled</span>
              </div>
            </article>

            <article className="boundary-card domain-card">
              <span className="eyebrow">DOMAIN BOUNDARY</span>
              <h2>Focused<br /><span>by design.</span></h2>
              <p>
                <strong>Clyptus.AI</strong> answers questions about <strong>SAP and SAP BRIM</strong>. Requests outside SAP
                are <strong>declined up front</strong>, before any retrieval or generation.
              </p>
              <div className="boundary-example">
                <strong>User</strong>
                <p>“What is the weather in Paris?”</p>
                <strong>Assistant</strong>
                <p>“I can only assist with SAP and SAP-related topics. Please ask questions related to SAP BRIM or other SAP modules.”</p>
              </div>
            </article>
          </div>
        </section>

        {/* Question Section */}
        <section className="section section-wrap question-section">
          <div className="question-heading">
            <span className="eyebrow">START WITH WHAT YOU'RE WORKING ON</span>
            <h2>Ask the SAP question<br /><span>already in front of you.</span></h2>
            <p>
              Ask about <strong>transaction codes</strong>, <strong>error messages</strong>, <strong>Customizing paths</strong>,
              <strong> process integration</strong> or a <strong>document you upload</strong>.
            </p>
          </div>
          <div className="question-list">
            {exampleQuestions.map((question, index) => (
              <div className="question-chip" key={question}>
                <span>0{index + 1}</span>
                <p>{question}</p>
                <ArrowUpRight size={15} aria-hidden="true" />
              </div>
            ))}
          </div>
        </section>

        {/* About Clyptus Section */}
        <section id="about" className="about-clyptus-section section-wrap">
          <div className="about-clyptus-inner">
            <div className="about-header">
              <span className="eyebrow">ABOUT CLYPTUS &amp; CLYPTUS.AI</span>
              <h2>
                Engineered for SAP precision.<br />
                <span>Less guesswork in critical billing processes.</span>
              </h2>
              <p className="about-lead">
                <strong>Clyptus Technologies</strong> builds domain-specialised AI for complex ERP landscapes.
                <strong> Clyptus.AI</strong> connects scattered SAP BRIM documentation to <strong>grounded, practical
                answers</strong> for solution architects, billing consultants and support teams.
              </p>
            </div>

            <div className="about-story-grid">
              <div className="about-story-card">
                <span className="story-badge">THE CHALLENGE</span>
                <h3>Why generic AI struggles with SAP</h3>
                <p>
                  General-purpose language models often invent transaction codes and Customizing paths, or mix up
                  Convergent Charging rating with Convergent Invoicing billing. In telecommunications, utilities and
                  subscription businesses, one wrong billing setting can hold up invoicing and revenue.
                </p>
              </div>

              <div className="about-story-card">
                <span className="story-badge">THE CLYPTUS APPROACH</span>
                <h3>Evidence before generation</h3>
                <p>
                  In the RAG Pipeline, every question goes through hybrid retrieval (dense embeddings, BM25 keyword ranking and
                  exact SAP identifier matching) before the model writes anything. The draft is then verified claim by
                  claim. If the evidence does not support it, the system says so instead of guessing.
                </p>
              </div>
            </div>

            <div className="about-pillars-grid">
              <div className="pillar-item">
                <div className="pillar-num">01</div>
                <h4>Curated knowledge base</h4>
                <p>Administrators upload and classify SAP documentation. Each document has a source-authority tier and access groups.</p>
              </div>

              <div className="pillar-item">
                <div className="pillar-num">02</div>
                <h4>Hybrid retrieval</h4>
                <p>Vector search on PostgreSQL pgvector, BM25 and identifier channels, merged with reciprocal rank fusion and reranked.</p>
              </div>

              <div className="pillar-item">
                <div className="pillar-num">03</div>
                <h4>Grounding verification</h4>
                <p>Claims, transaction codes and table names are validated against the evidence, with one controlled repair before a fail-closed response.</p>
              </div>

              <div className="pillar-item">
                <div className="pillar-num">04</div>
                <h4>Access control</h4>
                <p>Per-user chat isolation, document access groups, and short-lived signed sessions that can be revoked.</p>
              </div>
            </div>

            <div className="about-stats-band">
              <div className="stat-box">
                <strong>2 modes</strong>
                <span>RAG Pipeline by default, Direct LLM on demand</span>
              </div>
              <div className="stat-box">
                <strong>5 BRIM</strong>
                <span>components: SOM, CC, CI, FI-CA, CM</span>
              </div>
              <div className="stat-box">
                <strong>3 channels</strong>
                <span>Dense, BM25 and identifier retrieval</span>
              </div>
              <div className="stat-box">
                <strong>20 messages</strong>
                <span>of recent context, plus a running summary</span>
              </div>
            </div>
          </div>
        </section>

        {/* Use Cases */}
        <section className="usecase-section">
          <div className="section section-wrap">
            <div className="usecase-heading">
              <span className="eyebrow">SAP-SPECIFIC USE CASES</span>
              <h2>From questions<br /><span>to actionable technical clarity.</span></h2>
            </div>
            <div className="usecase-grid">
              {useCases.map(([title, text], i) => (
                <article key={title} className="usecase-card">
                  <span>0{i + 1}</span>
                  <h3>{title}</h3>
                  <p>{text}</p>
                </article>
              ))}
            </div>
          </div>
        </section>

        {/* FAQ Section */}
        <FaqSection />

        {/* Final CTA */}
        <section className="final-cta section-wrap">
          <div className="final-cta-inner">
            <span className="eyebrow">READY TO EXPLORE SAP?</span>
            <h2>Have an SAP question?<br /><span>Ask Clyptus.AI.</span></h2>
            <p>
              Get <strong>verified answers from the RAG Pipeline</strong> by default or switch to <strong>Direct LLM</strong>, across
              <strong> SAP and SAP BRIM</strong>.
            </p>

            <Link className="button button-brand" to="/login">
              Open workspace <ArrowUpRight size={17} />
            </Link>
          </div>
        </section>
      </main>
      <Footer />
    </>
  );
};
