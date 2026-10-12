// Local, seat-bound portraits: never derived from a secret role or model.
const colors = ['#d5ac81', '#aab9dc', '#a8c5b6', '#dfbbce', '#d4c788', '#c4b4dc', '#a7cbd7', '#d7a99e', '#b7c69c'];
const ears = [
  'M14 24 10 7 27 17 M37 17 54 7 50 24',
  'M14 24 15 5 27 17 M37 17 49 5 50 24',
  'M15 22 20 5 28 17 M36 17 44 5 49 22',
  'M19 23 18 3 27 3 28 19 M36 19 37 3 46 3 45 23',
  'M13 24 8 12 24 17 M40 17 56 12 51 24',
  'M15 23 10 8 28 18 M36 18 54 8 49 23',
  'M13 23 9 4 28 18 M36 18 55 4 51 23',
  'M16 24 20 8 28 18 M36 18 44 8 48 24',
  'M15 23 12 4 26 15 M38 15 52 4 49 23',
];
export function Avatar({ seat }: { seat: number }) {
  const index = (seat - 1) % colors.length;
  return <span className="seat-avatar" aria-hidden="true">
    <svg viewBox="0 0 64 64" focusable="false">
      <rect width="64" height="64" rx="18" fill="#23333b" />
      <path d={ears[index]} fill={colors[index]} stroke={colors[index]} strokeWidth="4" strokeLinejoin="round" />
      <ellipse cx="32" cy="33" rx="21" ry="21" fill={colors[index]} />
      <ellipse cx="32" cy="42" rx="13" ry="10" fill="#f1e8d7" />
      <path d="M21 30h2m18 0h2" stroke="#273038" strokeWidth="4" strokeLinecap="round" />
      <path d="M28 37h8l-4 6z" fill="#273038" />
      <path d="M32 43v3m-5 0q5 5 10 0" fill="none" stroke="#273038" strokeWidth="2" strokeLinecap="round" />
    </svg><span className="avatar-number">{seat}</span>
  </span>;
}
