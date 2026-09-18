import { useCallback, useEffect, useRef, useState } from 'react';
import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { api } from '../data/api.js';
import { exploreStore } from '../data/exploreStore.js';

const LAYERS = [
  { id: 'none', label: 'None', legend: null },
  {
    id: 'roof_mask',
    label: 'Roofs',
    legend: { kind: 'solid', colour: '#00e5ff', text: 'Roof pixels passing the height and confidence thresholds' },
  },
  {
    id: 'shadow_frequency',
    label: 'Shadow',
    legend: {
      kind: 'ramp',
      stops: ['#1e3a5f', '#3b82f6', '#fbbf24', '#ef4444'],
      low: '0%',
      high: '35%+',
      text: 'Insolation-weighted fraction of daylight hours in shade',
    },
  },
  {
    id: 'sky_view_factor',
    label: 'Sky view',
    legend: {
      kind: 'ramp',
      stops: ['#ef4444', '#f59e0b', '#22c55e'],
      low: '0.6',
      high: '1.0',
      text: 'Fraction of the sky hemisphere visible from the roof plane',
    },
  },
  {
    id: 'net_irradiance',
    label: 'Net irradiance',
    legend: {
      kind: 'ramp',
      stops: ['#1e3a5f', '#2563eb', '#22c55e', '#fbbf24'],
      low: '50% of baseline',
      high: '95%',
      text: 'Plane-of-array energy after all four penalties',
    },
  },
  {
    id: 'temperature_delta',
    label: 'Heat island',
    legend: {
      kind: 'ramp',
      stops: ['#2563eb', '#22c55e', '#a855f7', '#ef4444'],
      low: '−3 °C',
      high: '+8 °C',
      text: 'Land surface temperature above the 30 km rural background',
    },
  },
];

/**
 * Keyless basemaps.
 *
 * Imagery is the default. Building selection requires visual identification of
 * the roof, which a street basemap does not support: at 4 m analysis resolution
 * adjacent structures are separately resolved, so an incorrect click yields a
 * valid figure for the wrong building.
 *
 * CARTO is absent because it is not keyless. It returns tiles but overprints
 * "API KEY REQUIRED" on each one, producing a rendered map that is unreadable
 * without an error being raised.
 */
const BASEMAPS = {
  satellite: {
    label: 'Satellite',
    style: {
      version: 8,
      sources: {
        imagery: {
          type: 'raster',
          tiles: [
            'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
          ],
          tileSize: 256,
          maxzoom: 19,
          attribution: 'Imagery © Esri, Maxar, Earthstar Geographics',
        },
        labels: {
          type: 'raster',
          tiles: [
            'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',
          ],
          tileSize: 256,
          maxzoom: 19,
          attribution: '',
        },
      },
      layers: [
        { id: 'imagery', type: 'raster', source: 'imagery' },
        { id: 'labels', type: 'raster', source: 'labels', paint: { 'raster-opacity': 0.8 } },
      ],
    },
  },
  streets: { label: 'Streets', style: 'https://tiles.openfreemap.org/styles/dark' },
  light: { label: 'Light', style: 'https://tiles.openfreemap.org/styles/liberty' },
};

const RASTER_FALLBACK = {
  version: 8,
  sources: {
    osm: {
      type: 'raster',
      tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
      tileSize: 256,
      maxzoom: 19,
      attribution:
        '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    },
  },
  layers: [{ id: 'osm', type: 'raster', source: 'osm' }],
};

const VECTOR_ATTRIBUTION =
  '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors · ' +
  '<a href="https://openfreemap.org/">OpenFreeMap</a>';

/**
 * Vector layers, in draw order, bottom to top.
 *
 * Declared as data because the raster overlay must be inserted *below* all of
 * them. Adding it without `beforeId` placed it on top, concealing both the
 * area outline and the selected-building outline whenever an overlay was
 * active -- which is why the area guide appeared to be missing.
 */
const VECTOR_LAYER_IDS = ['aoi-casing', 'aoi-line', 'building-fill', 'building-line', 'pin'];

function aoiFeature(lat, lon, half) {
  const ring = [
    [lon - half, lat - half],
    [lon + half, lat - half],
    [lon + half, lat + half],
    [lon - half, lat + half],
    [lon - half, lat - half],
  ];
  return { type: 'Feature', geometry: { type: 'Polygon', coordinates: [ring] }, properties: {} };
}

function pointFeature(lat, lon) {
  return { type: 'Feature', geometry: { type: 'Point', coordinates: [lon, lat] }, properties: {} };
}

const EMPTY_FC = { type: 'FeatureCollection', features: [] };

/** Debounce for the automatic roof preview after a click. */
const PREVIEW_DELAY_MS = 450;

export default function MapPanel({ lat, lon, halfSize, onPick, request, building }) {
  const containerRef = useRef(null);
  const mapRef = useRef(null);
  const overlayRef = useRef(null);
  const buildingRef = useRef(building);
  const previewTimer = useRef(null);
  const [ready, setReady] = useState(false);
  const [basemap, setBasemap] = useState('satellite');
  const [layer, setLayer] = useState('roof_mask');
  const [layerBusy, setLayerBusy] = useState(false);
  const [layerError, setLayerError] = useState(null);
  const [basemapNote, setBasemapNote] = useState(null);
  const [cached, setCached] = useState(() => new Set());

  buildingRef.current = building;

  const attachLayers = useCallback(
    (map) => {
      if (!map.getSource('aoi')) {
        map.addSource('aoi', { type: 'geojson', data: aoiFeature(lat, lon, halfSize) });
      }
      if (!map.getSource('building')) {
        map.addSource('building', { type: 'geojson', data: buildingRef.current ?? EMPTY_FC });
      }
      if (!map.getSource('pin')) {
        map.addSource('pin', { type: 'geojson', data: pointFeature(lat, lon) });
      }

      // A dark casing under a bright dashed line. Satellite imagery ranges from
      // near-white rooftops to near-black shadow, so a single-colour hairline
      // disappears against one or the other; a casing keeps it legible on both.
      if (!map.getLayer('aoi-casing')) {
        map.addLayer({
          id: 'aoi-casing',
          type: 'line',
          source: 'aoi',
          paint: { 'line-color': '#000000', 'line-width': 4, 'line-opacity': 0.5 },
        });
      }
      if (!map.getLayer('aoi-line')) {
        map.addLayer({
          id: 'aoi-line',
          type: 'line',
          source: 'aoi',
          paint: {
            'line-color': '#ffc247',
            'line-width': 2,
            'line-dasharray': [2, 1.5],
          },
        });
      }
      if (!map.getLayer('building-fill')) {
        map.addLayer({
          id: 'building-fill',
          type: 'fill',
          source: 'building',
          paint: { 'fill-color': '#ffffff', 'fill-opacity': 0.1 },
        });
      }
      if (!map.getLayer('building-line')) {
        map.addLayer({
          id: 'building-line',
          type: 'line',
          source: 'building',
          paint: { 'line-color': '#ffffff', 'line-width': 2.5 },
        });
      }
      if (!map.getLayer('pin')) {
        map.addLayer({
          id: 'pin',
          type: 'circle',
          source: 'pin',
          paint: {
            'circle-radius': 6,
            'circle-color': '#ffc247',
            'circle-stroke-color': '#0a0e14',
            'circle-stroke-width': 2,
          },
        });
      }

      // Inserted beneath the vector layers, never over them.
      if (overlayRef.current && !map.getSource('overlay')) {
        const o = overlayRef.current;
        map.addSource('overlay', {
          type: 'raster',
          tiles: [o.urlTemplate],
          tileSize: o.tileSize ?? 256,
          minzoom: o.minZoom ?? 0,
          maxzoom: o.maxZoom ?? 19,
          attribution: o.attribution ?? 'Google Earth Engine',
        });
        const below = VECTOR_LAYER_IDS.find((id) => map.getLayer(id));
        map.addLayer(
          {
            id: 'overlay',
            type: 'raster',
            source: 'overlay',
            paint: { 'raster-opacity': 0.85 },
          },
          below,
        );
      }
    },
    [lat, lon, halfSize],
  );

  const applyOverlay = useCallback(
    (map, data) => {
      overlayRef.current = data;
      if (map.getLayer('overlay')) map.removeLayer('overlay');
      if (map.getSource('overlay')) map.removeSource('overlay');
      attachLayers(map);
    },
    [attachLayers],
  );

  /** Fetch a layer, preferring the cache. `quiet` suppresses the error banner. */
  const loadLayer = useCallback(
    async (layerId, { quiet = false } = {}) => {
      const map = mapRef.current;
      if (!map) return;
      setLayer(layerId);
      if (!quiet) setLayerError(null);

      if (layerId === 'none') {
        overlayRef.current = null;
        if (map.getLayer('overlay')) map.removeLayer('overlay');
        if (map.getSource('overlay')) map.removeSource('overlay');
        return;
      }

      const hit = exploreStore.getOverlay(layerId, request);
      if (hit) {
        applyOverlay(map, hit);
        return;
      }

      setLayerBusy(true);
      try {
        const { data } = await api.tiles({ ...request, layer: layerId });
        if (!data.urlTemplate) throw new Error('The tiles endpoint returned no urlTemplate.');
        exploreStore.setOverlay(layerId, request, data);
        setCached(exploreStore.cachedLayers(request));
        applyOverlay(map, data);
      } catch (err) {
        if (!quiet) {
          setLayerError(err.message ?? 'Could not load that layer.');
          setLayer('none');
        }
      } finally {
        setLayerBusy(false);
      }
    },
    [request, applyOverlay],
  );

  useEffect(() => {
    if (mapRef.current) return undefined;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: BASEMAPS.satellite.style,
      center: [lon, lat],
      zoom: 17,
      attributionControl: { compact: true, customAttribution: VECTOR_ATTRIBUTION },
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: 'metric' }));

    // Crosshair, not a grab hand. The map's primary interaction is selecting a
    // specific structure, and a pointer cursor gives no indication of which
    // pixel will be read.
    map.getCanvas().style.cursor = 'crosshair';

    map.on('load', () => {
      attachLayers(map);
      setReady(true);
    });
    map.on('styledata', () => {
      if (map.isStyleLoaded()) attachLayers(map);
    });
    map.on('error', (event) => {
      const message = String(event?.error?.message ?? '');
      if (message.includes('style') || message.includes('Failed to fetch')) {
        setBasemapNote('Basemap unavailable; using OpenStreetMap raster tiles.');
        try {
          map.setStyle(RASTER_FALLBACK);
        } catch {
          /* nothing further to try */
        }
      }
    });
    map.on('click', (event) => onPick(event.lngLat.lat, event.lngLat.lng));
    mapRef.current = map;
    return () => {
      map.remove();
      mapRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    map.setStyle(BASEMAPS[basemap].style);
  }, [basemap, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    map.getSource('aoi')?.setData(aoiFeature(lat, lon, halfSize));
    map.getSource('pin')?.setData(pointFeature(lat, lon));
    const centre = map.getCenter();
    if (Math.abs(centre.lat - lat) > 0.02 || Math.abs(centre.lng - lon) > 0.02) {
      map.easeTo({ center: [lon, lat], duration: 600 });
    }
  }, [lat, lon, halfSize, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    map.getSource('building')?.setData(building ?? EMPTY_FC);
  }, [building, ready]);

  const signature = `${lat}|${lon}|${halfSize}|${request.baseline_mode}|${request.year}|${request.month}|${request.quarter}`;

  /**
   * Refresh the active overlay when the selection changes.
   *
   * The roof mask defaults on and reloads automatically, so the roof the model
   * will actually read is visible **before** committing to a full computation.
   * Roof extraction is two Earth Engine calls against twelve for a yield
   * request, and identifying the wrong building is the failure mode that
   * invalidates everything downstream.
   *
   * Debounced: dragging the area slider or clicking repeatedly would otherwise
   * issue a request per event.
   */
  useEffect(() => {
    if (!ready) return undefined;
    if (layer === 'none') return undefined;
    clearTimeout(previewTimer.current);
    previewTimer.current = setTimeout(() => {
      loadLayer(layer, { quiet: true });
    }, PREVIEW_DELAY_MS);
    return () => clearTimeout(previewTimer.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature, ready]);

  useEffect(() => {
    setCached(exploreStore.cachedLayers(request));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature]);

  const active = LAYERS.find((l) => l.id === layer);

  return (
    <>
      <div ref={containerRef} style={{ position: 'absolute', inset: 0 }} />

      <div className="map-panel">
        <div className="map-panel__row">
          {Object.entries(BASEMAPS).map(([id, item]) => (
            <button
              key={id}
              className={`chip${basemap === id ? ' is-active' : ''}`}
              onClick={() => setBasemap(id)}
            >
              {item.label}
            </button>
          ))}
        </div>

        <div className="map-panel__label">
          Layer
          {layerBusy && <span className="spinner" style={{ marginLeft: '0.4rem' }} />}
        </div>
        <div className="map-panel__row">
          {LAYERS.map((item) => (
            <button
              key={item.id}
              className={`chip${layer === item.id ? ' is-active' : ''}`}
              onClick={() => loadLayer(item.id)}
              disabled={layerBusy}
              title={cached.has(item.id) ? 'Cached' : undefined}
            >
              {item.label}
              {cached.has(item.id) && item.id !== 'none' && (
                <span style={{ opacity: 0.55, marginLeft: '0.3rem' }}>·</span>
              )}
            </button>
          ))}
        </div>

        {active?.legend && (
          <div className="legend">
            {active.legend.kind === 'ramp' ? (
              <>
                <div
                  className="legend__ramp"
                  style={{
                    background: `linear-gradient(to right, ${active.legend.stops.join(', ')})`,
                  }}
                />
                <div className="legend__ends">
                  <span>{active.legend.low}</span>
                  <span>{active.legend.high}</span>
                </div>
              </>
            ) : (
              <div className="legend__swatch" style={{ background: active.legend.colour }} />
            )}
            <div className="legend__text">{active.legend.text}</div>
          </div>
        )}

        {layerError && <p className="map-panel__warn">{layerError}</p>}
        {basemapNote && <p className="map-panel__note">{basemapNote}</p>}
        <p className="map-panel__note">
          Click a structure to move the sample point. Raster layers render at the map&apos;s
          zoom; reported figures are reduced at 4 m.
        </p>
      </div>
    </>
  );
}
