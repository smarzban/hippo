import { flattenTree, writableFolders, type Folder, type UploadState } from "./folders";

type Props = {
  folders: Folder[];
  picked: number[];
  setPicked: React.Dispatch<React.SetStateAction<number[]>>;
  pickFile: File | null;
  setPickFile: (f: File | null) => void;
  up: UploadState;
  onUpload: () => void;
  onClose: () => void;
};

/** "Add a document" modal: file picker + writable-folder checkboxes, posts to
 * /ingest (one document per destination folder). Progress + errors come from the
 * uploadReducer state in App. */
export default function UploadModal({
  folders, picked, setPicked, pickFile, setPickFile, up, onUpload, onClose,
}: Props) {
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h3>Add a document</h3>
        <input type="file" accept=".md,.markdown,.txt,.html,.htm,.docx"
          onChange={(e) => setPickFile(e.target.files?.[0] ?? null)} />
        <p>Destination folders</p>
        <div className="dest-list">
          {flattenTree(writableFolders(folders)).map((f) => (
            <label key={f.id} style={{ paddingLeft: f.depth * 12 }}>
              <input type="checkbox" checked={picked.includes(f.id)}
                onChange={(e) => setPicked((p) =>
                  e.target.checked ? [...p, f.id] : p.filter((x) => x !== f.id))} />
              {f.name} <span className="sec">{f.tier}</span>
            </label>
          ))}
        </div>
        {up.status === "uploading" && <p>Uploading… {up.done}/{up.dests.length}</p>}
        {up.status === "error" && <p className="error">{up.error}</p>}
        {up.status === "done"
          ? <button onClick={onClose}>Done</button>
          : <button disabled={!pickFile || picked.length === 0 || up.status === "uploading"}
              onClick={onUpload}>Upload</button>}
      </div>
    </div>
  );
}
