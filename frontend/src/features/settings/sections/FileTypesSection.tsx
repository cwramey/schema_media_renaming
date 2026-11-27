import { useSettings } from '@/api/hooks';
import type { AppSettings } from '@/api/types';
import { useSaveSetting } from '../api';
import { ChipsEditor, Row, RowSkeleton, SectionError, SettingsSection } from '../controls';
import { checkExtension, normalizeExtension, normalizePattern, rowIds, type ChipCheck } from '../mapping';

type ListKey = 'video_extensions' | 'sidecar_extensions' | 'ignore_patterns';

const ROWS: {
  key: ListKey;
  label: string;
  help: string;
  placeholder: string;
  normalize: (s: string) => string;
  check?: ChipCheck;
  noun: string;
  what: string;
}[] = [
  {
    key: 'video_extensions',
    label: 'Video files',
    help: 'Files ending in these are treated as videos and matched.',
    placeholder: 'mkv',
    normalize: normalizeExtension,
    check: checkExtension,
    noun: 'video file type',
    what: 'the video file types',
  },
  {
    key: 'sidecar_extensions',
    label: 'Companion files',
    help: 'Subtitles and info files that move with their video when "Bring subtitles along" is on.',
    placeholder: 'srt',
    normalize: normalizeExtension,
    check: checkExtension,
    noun: 'companion file type',
    what: 'the companion file types',
  },
  {
    key: 'ignore_patterns',
    label: 'Always ignore',
    help: 'Unfinished downloads and temp files. Schema never touches anything matching these.',
    placeholder: '*.part',
    normalize: normalizePattern,
    noun: 'pattern',
    what: 'the ignore list',
  },
];

export function FileTypesSection() {
  const settings = useSettings();
  const saveSetting = useSaveSetting();
  const s: AppSettings | undefined = settings.data;
  return (
    <SettingsSection
      id="files"
      num={7}
      title="File types"
      description="Which files count as videos, which travel with them, and which are always ignored."
    >
      {settings.isPending ? (
        <RowSkeleton rows={3} />
      ) : settings.isError || !s ? (
        <SectionError error={settings.error} onRetry={() => void settings.refetch()} what="settings" />
      ) : (
        ROWS.map((r) => {
          const ids = rowIds(`set-${r.key.replace(/_/g, '-')}`);
          return (
            <Row key={r.key} id={ids.id} group label={r.label} help={r.help}>
              <ChipsEditor
                id={ids.id}
                labelledBy={ids.labelId}
                describedBy={ids.helpId}
                value={s[r.key]}
                onChange={(v) => saveSetting(r.key, v, r.what)}
                normalize={r.normalize}
                check={r.check}
                placeholder={r.placeholder}
                noun={r.noun}
              />
            </Row>
          );
        })
      )}
    </SettingsSection>
  );
}
