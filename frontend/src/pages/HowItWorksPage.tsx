import React from 'react';
import { ArrowDown, ArrowUpRight, Boxes, MessageCircle, Search, ShieldCheck, Sparkles } from 'lucide-react';
import { Link } from 'react-router-dom';
import { Footer } from '../components/footer/Footer';
import { PublicNav } from '../components/navbar/PublicNav';
import { PageMeta } from '../components/ui/PageMeta';

const steps = [
  [MessageCircle, 'Ask', 'Ask your SAP or BRIM question naturally, whether regarding a transaction code, configuration path, process flow, or customizing table.', 'No special syntax required.'],
  [Search, 'Retrieve', 'The RAG Pipeline searches indexed SAP documentation through dense vector similarity (sentence-embedding model), BM25 keyword ranking, and exact matching of SAP identifiers such as transaction codes and tables.', 'Hybrid retrieval.'],
  [Boxes, 'Fuse & Rerank', 'Reciprocal rank fusion merges the three channels; multi-signal reranking promotes passages whose sections, phrases and identifiers match the question. Weak results trigger a search of approved SAP web sources.', 'Evidence selection.'],
  [ShieldCheck, 'Generate & Verify', 'The LLM drafts an answer from the selected evidence only. Each claim and SAP identifier is checked against that evidence; unsupported statements are repaired or removed, and anything that still cannot be confirmed is labelled.', 'Claim-level grounding.'],
  [Sparkles, 'Stream', 'The verified answer streams to the chat over Server-Sent Events (SSE), with suggested follow-up questions. Direct LLM skips retrieval and streams directly for faster replies.', 'Delivered once verified.'],
] as const;

export const HowItWorksPage: React.FC = () => {
  return (
    <>
      <PageMeta
        title="How It Works"
        description="Learn how Clyptusap.ai retrieves SAP knowledge to generate grounded answers."
      />
      <PublicNav />
      <main className="inner-page">
        <section className="inner-hero how-hero section-wrap">
          <span className="eyebrow">RETRIEVAL-AUGMENTED GENERATION</span>
          <h1>
            From question<br />
            to <span>grounded answer.</span>
          </h1>
          <p>
            Clyptusap.ai searches its indexed SAP knowledge base for relevant technical documentation and uses that context to generate grounded enterprise answers under strict administrative control.
          </p>
          <a className="scroll-hint inner-scroll" href="#process">
            FOLLOW THE ARCHITECTURE <ArrowDown size={14} />
          </a>
          <div className="how-hero-flow">
            <span><MessageCircle size={17} /> Question</span>
            <ArrowUpRight size={16} />
            <span><Search size={17} /> Hybrid Retrieval</span>
            <ArrowUpRight size={16} />
            <span><Sparkles size={17} /> Verified Answer</span>
          </div>
        </section>


        <section className="section section-wrap how-process" id="process">
          <span className="eyebrow">FROM QUESTION TO RESPONSE</span>
          <div className="how-steps">
            {steps.map(([Icon, title, text, note], i) => (
              <article className="how-step" key={title}>
                <span className="how-step-number">0{i + 1}</span>
                <div className="how-step-icon"><Icon size={20} /></div>
                <div>
                  <h2>{title}</h2>
                  <p>{text}</p>
                  <span className="how-step-note">{note}</span>
                </div>
              </article>
            ))}
          </div>
        </section>

        <section className="inner-bottom-cta">
          <span className="eyebrow">READY TO TEST?</span>
          <h2>Experience the SAP Knowledge Assistant</h2>
          <Link className="button button-dark" to="/login">
            Ask Clyptusap.ai <ArrowUpRight size={16} />
          </Link>
        </section>
      </main>
      <Footer />
    </>
  );
};
