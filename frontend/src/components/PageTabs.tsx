/** Top-level tabs under a page header, e.g. "Findings | History". */
export function PageTabs<T extends string>({ value, options, onChange }: { value: T; options: { value: T; label: string }[]; onChange: (next: T) => void }) {
  return (
    <div className="tabs page-tabs" role="tablist">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          role="tab"
          aria-selected={value === option.value}
          className={`tab${value === option.value ? ' is-active' : ''}`}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}
