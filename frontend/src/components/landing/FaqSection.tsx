import React from 'react';
import { Plus } from 'lucide-react';

const faqs: Array<[string, React.ReactNode]> = [
  ['What is Clyptus.AI?', <><strong>Clyptus.AI</strong> is an AI knowledge assistant for SAP, with a dedicated focus on <strong>SAP BRIM</strong>: Subscription Order Management, Convergent Charging, Convergent Invoicing, FI-CA and Convergent Mediation.</>],
  ['What is the difference between the RAG Pipeline and Direct LLM?', <>Every chat starts with the <strong>RAG Pipeline</strong>, which retrieves from the SAP knowledge base, verifies the draft against that evidence, and only then shows the answer. Press <strong>High</strong> for a quick response from the configured language model. Both modes stay within SAP topics.</>],
  ['What does “retrieval-augmented generation” mean?', <><strong>Retrieval-augmented generation (RAG)</strong> finds relevant documentation first and gives it to the model as context, so the answer is based on sources rather than on the model's memory alone. The RAG Pipeline adds a <strong>claim-level grounding check</strong>, which reduces unsupported details.</>],
  ['Where do RAG Pipeline answers come from?', <>From an <strong>administrator-curated knowledge base</strong> of SAP documentation. When internal evidence is weak, the RAG Pipeline can also search <strong>approved SAP sources</strong> such as SAP Help Portal, SAP Community and SAP Learning.</>],
  ['Does it remember earlier messages?', <>Yes, in both modes. It uses the <strong>last 20 messages</strong>, a <strong>running summary</strong> of older turns, and can recall what you asked earlier in this chat or in your other chats. Your conversations are private to your account unless you share a prompt link.</>],
  ['Can I upload my own documents?', <>Yes. Attach <strong>PDF, Word, text or Markdown</strong> files to a message and ask about them. Uploaded text is used as context within that chat and is not added to the shared knowledge base.</>],
  ['Will I see the source documents?', <>Internal documents are <strong>managed and access-controlled by administrators</strong>. Regular users receive the verified answer and suggested follow-up questions; source excerpts and retrieval scores are available to administrators for review.</>],
  ['What if it cannot answer?', <>Questions outside SAP are <strong>declined up front</strong>. In the RAG Pipeline, if the evidence does not support an answer, it <strong>says so instead of guessing</strong>.</>],
];

export const FaqSection: React.FC = () => {
  return (
    <section className="section faq-section" id="faq">
      <div className="section-heading">
        <span className="eyebrow">A FEW GOOD QUESTIONS</span>
        <h2>Curious? <span>We thought so.</span></h2>
      </div>
      <div className="faq-list">
        {faqs.map(([question, answer]) => (
          <details className="faq-item" key={question}>
            <summary>
              {question}
              <Plus size={18} aria-hidden="true" />
            </summary>
            <p>{answer}</p>
          </details>
        ))}
      </div>
    </section>
  );
};
