export function EnterpriseBrandMark({ size = 48 }: { size?: number }) {
  return <span className="enterprise-brand-mark" style={{ width: size, height: size }} aria-hidden="true">
    <svg viewBox="0 0 1024 1024" role="img">
      <path className="brand-body" d="M608.768 98.742857a256 256 0 0 0 308.589714 390.070857v257.901715L512 981.357714 106.642286 746.788571V277.357714L512 42.715429l96.768 56.027428zM192 697.490286L512 882.834286V512L192 326.802286v370.834285z" />
      <path className="brand-spark" d="M833.024 444.342857l19.382857-44.470857a171.300571 171.300571 0 0 1 87.113143-88.356571l53.394286-23.698286a21.942857 21.942857 0 0 0 0-39.789714l-51.712-23.04a171.373714 171.373714 0 0 1-88.356572-91.282286l-19.748571-47.396572a20.918857 20.918857 0 0 0-38.765714 0l-19.748572 47.396572a171.373714 171.373714 0 0 1-88.356571 91.282286l-51.785143 23.04a21.942857 21.942857 0 0 0 0 39.789714l53.394286 23.698286c38.765714 17.261714 69.924571 48.713143 87.186285 88.356571l19.382857 44.470857a20.918857 20.918857 0 0 0 38.619429 0z" />
    </svg>
  </span>;
}

export function EnterpriseBrandLockup({ compact = false }: { compact?: boolean }) {
  return <span className={`enterprise-brand-lockup${compact ? " compact" : ""}`}>
    <EnterpriseBrandMark size={compact ? 42 : 50} />
    <span><small>GEO INTELLIGENCE</small><b>品牌推荐诊断</b></span>
  </span>;
}
