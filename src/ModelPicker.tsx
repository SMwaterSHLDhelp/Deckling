import { PanelSectionRow, TextField } from "@decky/ui";
import { useState } from "react";
import { DeckRow } from "./DeckRow";
import { fieldValue } from "./form";

export function ModelPicker({
  label,
  models,
  value,
  onChange,
  onRefresh,
  loading,
  error,
  visionIds,
}: {
  label: string;
  models: string[];
  value: string;
  onChange: (model: string) => void;
  onRefresh: () => void;
  loading: boolean;
  error: string;
  visionIds?: string[];
}) {
  const [custom, setCustom] = useState(false);
  const inList = models.includes(value);
  const showCustom = models.length === 0 || custom || (Boolean(value) && !inList);
  let status = "Save the provider, then Refresh models.";
  if (loading) {
    status = "Loading models…";
  } else if (models.length > 0) {
    status = "Pick a model from the list.";
  }
  return (
    <>
      <PanelSectionRow>
        <div>{status}</div>
      </PanelSectionRow>
      {error ? (
        <PanelSectionRow>
          <div style={{ color: "#f2b8b5", whiteSpace: "pre-wrap" }}>{error}</div>
        </PanelSectionRow>
      ) : null}
      {models.map((id) => {
        const sees = visionIds?.includes(id) ? " · sees the screen" : "";
        return (
          <DeckRow
            key={id}
            layout="below"
            onClick={() => {
              setCustom(false);
              onChange(id);
            }}
          >
            {value === id ? `Selected: ${id}${sees}` : `${id}${sees}`}
          </DeckRow>
        );
      })}
      {models.length > 0 ? (
        <DeckRow layout="below" onClick={() => setCustom(true)}>
          Custom model id
        </DeckRow>
      ) : null}
      {showCustom ? (
        <PanelSectionRow>
          <TextField label={label} value={value} onChange={(event) => onChange(fieldValue(event))} />
        </PanelSectionRow>
      ) : null}
      <DeckRow layout="below" onClick={onRefresh}>
        {loading ? "Refreshing models…" : "Refresh models"}
      </DeckRow>
    </>
  );
}
