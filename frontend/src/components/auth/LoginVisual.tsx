import React from 'react';

/**
 * SAP BRIM flow (SOM → CC → CI → FI-CA) feeding a verified answer. Inline SVG so its neutrals follow
 * the page theme through the --lv-* custom properties set on `.auth-visual` in index.css; the brand
 * orange stays fixed.
 */
export const LoginVisual: React.FC<{ className?: string }> = ({ className }) => (
  <svg
    className={className}
    viewBox="0 0 560 380"
    role="img"
    aria-label="SAP BRIM flow from subscription orders through charging and invoicing to contract accounting, ending in a verified answer"
  >
    <defs>
      <linearGradient id="lv-flow" x1="0" y1="0" x2="1" y2="0">
        <stop offset="0" stopColor="#ff6600" stopOpacity="0.3" />
        <stop offset="0.5" stopColor="#ff6600" />
        <stop offset="1" stopColor="#ff8a38" stopOpacity="0.4" />
      </linearGradient>
      <radialGradient id="lv-halo" cx="0.5" cy="0.5" r="0.5">
        <stop offset="0" stopColor="#ff6600" stopOpacity="0.35" />
        <stop offset="1" stopColor="#ff6600" stopOpacity="0" />
      </radialGradient>
      <filter id="lv-soft" x="-20%" y="-20%" width="140%" height="140%">
        <feGaussianBlur stdDeviation="12" />
      </filter>
    </defs>

    <g className="lv-grid">
      <path d="M0 30H560M0 90H560M0 150H560M0 210H560M0 270H560M0 330H560" />
      <path d="M40 0V380M120 0V380M200 0V380M280 0V380M360 0V380M440 0V380M520 0V380" />
    </g>

    <path d="M70 82H490" fill="none" stroke="url(#lv-flow)" strokeWidth="2.5" />
    <path d="M280 110V186" fill="none" stroke="#ff6600" strokeOpacity="0.6" strokeWidth="2" strokeDasharray="4 6" />

    <g textAnchor="middle" className="lv-label">
      {[
        { x: 28, code: 'SOM', name: 'Orders', accent: true },
        { x: 133, code: 'CC', name: 'Charging', accent: false },
        { x: 343, code: 'CI', name: 'Invoicing', accent: false },
        { x: 448, code: 'FI-CA', name: 'Accounting', accent: true },
      ].map(({ x, code, name, accent }) => (
        <g key={code}>
          <rect x={x} y="54" width="84" height="56" rx="12" className={accent ? 'lv-node lv-node-accent' : 'lv-node'} />
          <text x={x + 42} y="80" fontSize="15" fontWeight="700" className="lv-text">{code}</text>
          <text x={x + 42} y="97" fontSize="9.5" className="lv-muted">{name}</text>
        </g>
      ))}
    </g>

    <circle cx="280" cy="82" r="48" fill="url(#lv-halo)" />
    <circle cx="280" cy="82" r="27" className="lv-hub" stroke="#ff6600" strokeWidth="2.5" />
    <path d="M291 70 A 15 15 0 1 0 291 94" fill="none" stroke="#ff6600" strokeWidth="4.5" strokeLinecap="round" />

    <g fill="#ff6600">
      <circle cx="122" cy="82" r="3" />
      <circle cx="227" cy="82" r="3" />
      <circle cx="333" cy="82" r="3" />
      <circle cx="438" cy="82" r="3" />
    </g>

    <rect x="150" y="204" width="260" height="140" rx="16" fill="#ff6600" opacity="0.14" filter="url(#lv-soft)" />
    <rect x="150" y="190" width="260" height="150" rx="16" className="lv-card" />
    <circle cx="176" cy="216" r="9" fill="#ff6600" />
    <path d="M171.5 216.5l3 3l6-6.5" fill="none" stroke="#0f172a" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
    <text x="194" y="220" fontSize="12.5" fontWeight="700" className="lv-text lv-label">Verified answer</text>
    <text x="392" y="220" fontSize="9.5" textAnchor="end" fill="#ff6600" className="lv-label">RAG</text>
    <g fontSize="9.5" className="lv-muted lv-label">
      <text x="170" y="247"><tspan className="lv-text" fontWeight="700">SOM</tspan> captures the subscription order</text>
      <text x="170" y="263"><tspan className="lv-text" fontWeight="700">CC</tspan> rates usage and recurring fees</text>
      <text x="170" y="279"><tspan className="lv-text" fontWeight="700">CI</tspan> bills them into one invoice</text>
      <text x="170" y="295"><tspan className="lv-text" fontWeight="700">FI-CA</tspan> posts it to the ledger</text>
    </g>
    <g fontSize="9.5" textAnchor="middle" className="lv-label">
      <rect x="170" y="308" width="78" height="18" rx="9" fill="#ff6600" fillOpacity="0.14" stroke="#ff6600" strokeOpacity="0.45" />
      <text x="209" y="320.5" fill="#ff6600">Claim-checked</text>
      <rect x="256" y="308" width="68" height="18" rx="9" className="lv-chip" />
      <text x="290" y="320.5" className="lv-muted">SAP scope</text>
    </g>
  </svg>
);
