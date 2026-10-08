import { useEffect, useState } from "react";

interface Props {
  files: Record<string, string>;
  emptyHint?: string;
}

/** Tabbed read-only viewer for generated files. */
export function FileViewer({ files, emptyHint = "No files generated." }: Props) {
  const names = Object.keys(files).sort();
  const [active, setActive] = useState<string>(names[0] ?? "");

  useEffect(() => {
    if (!names.includes(active)) setActive(names[0] ?? "");
  }, [names.join(","), active]);

  if (names.length === 0) return <p className="empty">{emptyHint}</p>;

  return (
    <div>
      <div className="file-list">
        {names.map((name) => (
          <button
            key={name}
            className={name === active ? "active" : ""}
            onClick={() => setActive(name)}
          >
            {name}
          </button>
        ))}
      </div>
      <pre className="code">{files[active]}</pre>
    </div>
  );
}