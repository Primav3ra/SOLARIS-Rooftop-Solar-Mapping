/**
 * The evaluation cities, mirrored from `solaris.evals.references.CITIES`.
 *
 * Duplicated deliberately and narrowly: these are used for map shortcuts, and
 * fetching them would put an API call in front of the first paint of the
 * Explore page. Only the fields the map needs are here -- no coefficients, no
 * thresholds. Anything that affects a *computation* is fetched from
 * `/api/presets` instead, because that is where duplication actually causes
 * drift.
 */

export const CITIES = [
  { key: 'delhi', name: 'Delhi', lat: 28.6139, lon: 77.209, zone: 'composite, very high aerosol' },
  { key: 'mumbai', name: 'Mumbai', lat: 19.076, lon: 72.8777, zone: 'coastal, humid' },
  { key: 'bengaluru', name: 'Bengaluru', lat: 12.9716, lon: 77.5946, zone: 'plateau, moderate' },
  { key: 'chennai', name: 'Chennai', lat: 13.0827, lon: 80.2707, zone: 'coastal tropical' },
  { key: 'kolkata', name: 'Kolkata', lat: 22.5726, lon: 88.3639, zone: 'humid subtropical' },
  { key: 'jaipur', name: 'Jaipur', lat: 26.9124, lon: 75.7873, zone: 'semi-arid, high aerosol' },
  { key: 'jodhpur', name: 'Jodhpur', lat: 26.2389, lon: 73.0243, zone: 'arid, dusty' },
  { key: 'ahmedabad', name: 'Ahmedabad', lat: 23.0225, lon: 72.5714, zone: 'semi-arid' },
  { key: 'guwahati', name: 'Guwahati', lat: 26.1445, lon: 91.7362, zone: 'high cloud, north-east' },
  { key: 'leh', name: 'Leh', lat: 34.1526, lon: 77.5771, zone: 'high altitude, low aerosol' },
];

export const CITY_BY_KEY = Object.fromEntries(CITIES.map((c) => [c.key, c]));
