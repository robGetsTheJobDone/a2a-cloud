export type WriteScopeLike = {
  outputs_prefix?: string | null;
  write_prefix?: string | null;
  write_prefixes?: string[] | null;
};

export function writeScopeLabels(scope: WriteScopeLike): string[] {
  const labels = Array.isArray(scope.write_prefixes)
    ? scope.write_prefixes.filter(Boolean)
    : [];
  if (labels.length === 0 && scope.write_prefix) labels.push(scope.write_prefix);
  if (labels.length === 0 && scope.outputs_prefix) labels.push(scope.outputs_prefix);
  return labels;
}
