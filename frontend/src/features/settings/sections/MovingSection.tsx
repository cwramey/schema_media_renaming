import { useSettings } from '@/api/hooks';
import { useSaveSetting } from '../api';
import { RadioCards, Row, RowSkeleton, SectionError, Segmented, SettingsSection, ToggleRow } from '../controls';
import { CONFLICT_CARDS, MODE_CARDS, clearSolvedOptions, selectedKey, CLEAR_SOLVED_DEFAULT } from '../mapping';

export function MovingSection() {
  const settings = useSettings();
  const saveSetting = useSaveSetting();
  const s = settings.data;

  return (
    <SettingsSection id="moving" num={4} title="Moving files" description="What physically happens to a file once it’s matched.">
      {settings.isPending ? (
        <RowSkeleton rows={5} />
      ) : settings.isError || !s ? (
        <SectionError error={settings.error} onRetry={() => void settings.refetch()} what="settings" />
      ) : (
        <>
          <Row id="set-mode" group width="wide" label="What to do with the file" help="Pick how the file gets into your library.">
            <RadioCards
              id="set-mode"
              name="default_mode"
              labelledBy="set-mode-label"
              describedBy="set-mode-help"
              options={MODE_CARDS}
              value={s.default_mode}
              onChange={(v) => saveSetting('default_mode', v, 'what to do with files')}
            />
          </Row>
          <Row
            id="set-conflict"
            group
            width="wide"
            label="If a file with that name already exists"
            help='This is what caused the "Destination already exists" errors from the bonus clips.'
          >
            <RadioCards
              id="set-conflict"
              name="conflict_policy"
              labelledBy="set-conflict-label"
              describedBy="set-conflict-help"
              options={CONFLICT_CARDS}
              value={s.conflict_policy}
              onChange={(v) => saveSetting('conflict_policy', v, 'the name clash setting')}
            />
          </Row>
          <ToggleRow
            id="set-move-sidecars"
            label="Bring subtitles and info files along"
            help="Files like .srt subtitles and .nfo info files move with their video and get matching names."
            checked={s.move_sidecars}
            onChange={(v) => saveSetting('move_sidecars', v, 'the subtitles setting')}
          />
          <ToggleRow
            id="set-remove-empty"
            label="Delete empty download folders afterwards"
            help="Once the video is moved out, the leftover empty folder in downloads is removed."
            checked={s.remove_empty_source_folders}
            onChange={(v) => saveSetting('remove_empty_source_folders', v, 'the empty folder setting')}
          />
          <ToggleRow
            id="set-clean-releases"
            label="Clean up download folders afterwards"
            help="Once every video in a download folder is in your library (samples and extras skipped), the folder and its leftovers (.nfo, pictures, text files, extras) are moved aside and deleted a day later."
            checked={s.clean_release_folders ?? true}
            onChange={(v) => saveSetting('clean_release_folders', v, 'the download cleanup setting')}
          />
          <ClearSolvedRow
            value={s.clear_solved_after_hours === undefined ? CLEAR_SOLVED_DEFAULT : s.clear_solved_after_hours}
            onChange={(v) => saveSetting('clear_solved_after_hours', v, 'when solved problems clear')}
          />
        </>
      )}
    </SettingsSection>
  );
}

function ClearSolvedRow({ value, onChange }: { value: number | null; onChange: (v: number | null) => void }) {
  const opts = clearSolvedOptions(value);
  return (
    <Row
      id="set-clear-solved"
      group
      label="Clear solved problems from Home"
      help="Old errors you’ve already dealt with stop cluttering Home. They stay in History."
    >
      <Segmented
        id="set-clear-solved"
        labelledBy="set-clear-solved-label"
        describedBy="set-clear-solved-help"
        options={opts.map((o) => ({ value: o.key, label: o.label }))}
        value={selectedKey(opts, value)}
        onChange={(key) => {
          const o = opts.find((x) => x.key === key);
          if (o) onChange(o.value);
        }}
      />
    </Row>
  );
}
