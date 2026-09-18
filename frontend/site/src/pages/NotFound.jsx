import { Link } from 'react-router-dom';

export default function NotFound() {
  return (
    <div className="page">
      <h1>Not found</h1>
      <p className="lede">
        There is no page at this address. If you followed a link from somewhere, the
        route may have been renamed.
      </p>
      <div style={{ display: 'flex', gap: '0.75rem', flexWrap: 'wrap', marginTop: '1.5rem' }}>
        <Link className="btn btn--primary" to="/">
          Back to the start
        </Link>
        <Link className="btn" to="/explore">
          Go to the map
        </Link>
      </div>
    </div>
  );
}
