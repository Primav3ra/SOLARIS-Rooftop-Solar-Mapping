function buildRasterStyle(kind) {
  const sources = {
    street: {
      tiles: [
        'https://a.tile.openstreetmap.org/{z}/{x}/{y}.png',
        'https://b.tile.openstreetmap.org/{z}/{x}/{y}.png',
        'https://c.tile.openstreetmap.org/{z}/{x}/{y}.png',
      ],
      attr: '&copy; OpenStreetMap contributors',
    },
    satellite: {
      tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],
      attr: 'Tiles &copy; Esri',
    },
    hybrid: {
      tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],
      attr: 'Tiles &copy; Esri',
    },
  };

  const cfg = sources[kind] || sources.street;
  return {
    version: 8,
    sources: {
      basemap: {
        type: 'raster',
        tiles: cfg.tiles,
        tileSize: 256,
        // Prevent "map data not yet available" at extreme zoom levels.
        // OSM/Esri raster endpoints typically top out at z=19.
        maxzoom: 19,
        attribution: cfg.attr,
      },
    },
    layers: [{ id: 'basemap', type: 'raster', source: 'basemap' }],
  };
}

function getAoiFillColor(irradiance) {
  // Keep AOI highlight stable/readable.
  // The yellow/orange tint previously came from irradiance/net-irradiance visualization.
  // We now show that as an optional raster layer (e.g., Temperature) instead.
  return '#2563eb';
}

export function createMapController() {
  let map = new maplibregl.Map({
    container: 'map',
    style: buildRasterStyle('street'),
    center: [77.09, 28.62],
    zoom: 14,
    maxZoom: 19,
    attributionControl: true,
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: true }), 'top-left');

  let clickCb = null;
  let savedPointCb = null;
  let overlays = [];
  let currentBasemap = 'street';
  let mapbox3dEnabled = false;
  let aoiState = null;
  let buildingState = null;
  let savedPointsState = { points: [], activeId: null };

  function ensureSource(id, source) {
    if (!map.getSource(id)) map.addSource(id, source);
  }

  function ensureLayer(layer, beforeId = null) {
    if (!map.getLayer(layer.id)) {
      if (beforeId && map.getLayer(beforeId)) map.addLayer(layer, beforeId);
      else map.addLayer(layer);
    }
  }

  function ensureSavedPointsLayer() {
    ensureSource('saved-points', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
    ensureLayer({
      id: 'saved-points-circle',
      type: 'circle',
      source: 'saved-points',
      paint: {
        'circle-radius': 10,
        'circle-color': [
          'case',
          ['==', ['get', 'id'], savedPointsState.activeId ?? ''],
          '#ffb347',
          '#2563eb',
        ],
        'circle-opacity': 0.92,
        'circle-stroke-width': 2,
        'circle-stroke-color': '#ffffff',
      },
    });
    ensureLayer({
      id: 'saved-points-label',
      type: 'symbol',
      source: 'saved-points',
      layout: {
        'text-field': ['get', 'label'],
        'text-size': 12,
        'text-font': ['Open Sans Bold', 'Arial Unicode MS Bold'],
        'text-offset': [0, 0],
        'text-allow-overlap': true,
      },
      paint: {
        'text-color': '#0b1020',
      },
    });
    map.on('mouseenter', 'saved-points-circle', () => { map.getCanvas().style.cursor = 'pointer'; });
    map.on('mouseleave', 'saved-points-circle', () => { map.getCanvas().style.cursor = ''; });
  }

  function setBasemap(name) {
    if (mapbox3dEnabled) return;
    currentBasemap = name;
    map.setStyle(buildRasterStyle(name));
    map.once('styledata', () => {
      map.setMaxZoom(19);
      if (aoiState) drawAOI(aoiState.lat, aoiState.lon, aoiState.halfSizeDeg);
      if (buildingState) renderBuildingLayer(buildingState.geojson);
      if (savedPointsState?.points?.length) renderSavedPoints(savedPointsState.points, savedPointsState.activeId);
      overlays.forEach((ov) => addRasterOverlay(ov.id, ov.urlTemplate, ov.opacity, ov.beforeId));
      if (clickCb || savedPointCb) attachClickDispatcher();
    });
    const temperatureEnabled = overlays.some((o) => o.id === 'temperature');
    document.querySelectorAll('.map-toggle button').forEach((b) => b.classList.remove('active'));
    document.getElementById(`btn${name.charAt(0).toUpperCase()}${name.slice(1)}`)?.classList.add('active');
    if (temperatureEnabled) document.getElementById('btnTemperature')?.classList.add('active');
  }

  function drawAOI(lat, lon, halfSizeDeg, irradiance = null) {
    const rect = {
      type: 'Feature',
      geometry: {
        type: 'Polygon',
        coordinates: [[
          [lon - halfSizeDeg, lat - halfSizeDeg],
          [lon + halfSizeDeg, lat - halfSizeDeg],
          [lon + halfSizeDeg, lat + halfSizeDeg],
          [lon - halfSizeDeg, lat + halfSizeDeg],
          [lon - halfSizeDeg, lat - halfSizeDeg],
        ]],
      },
      properties: {},
    };
    const point = {
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [lon, lat] },
      properties: {},
    };
    aoiState = { lat, lon, halfSizeDeg, irradiance };

    const fillColor = getAoiFillColor(irradiance);
    ensureSource('aoi', { type: 'geojson', data: { type: 'FeatureCollection', features: [rect] } });
    ensureLayer({
      id: 'aoi-fill',
      type: 'fill',
      source: 'aoi',
      paint: { 'fill-color': fillColor, 'fill-opacity': 0.16 },
    });
    ensureLayer({
      id: 'aoi-line',
      type: 'line',
      source: 'aoi',
      paint: { 'line-color': fillColor, 'line-width': 2 },
    });
    map.getSource('aoi')?.setData({ type: 'FeatureCollection', features: [rect] });
    map.setPaintProperty('aoi-fill', 'fill-color', fillColor);
    map.setPaintProperty('aoi-line', 'line-color', fillColor);

    ensureSource('aoi-point', { type: 'geojson', data: { type: 'FeatureCollection', features: [point] } });
    ensureLayer({
      id: 'aoi-point-layer',
      type: 'circle',
      source: 'aoi-point',
      paint: { 'circle-radius': 5, 'circle-color': '#ffffff', 'circle-stroke-color': '#2563eb', 'circle-stroke-width': 2 },
    });
    map.getSource('aoi-point')?.setData({ type: 'FeatureCollection', features: [point] });
  }

  function setAoiIntensity(irradiance) {
    if (!aoiState) return;
    drawAOI(aoiState.lat, aoiState.lon, aoiState.halfSizeDeg, irradiance);
  }

  function boundsFromGeojson(geojson) {
    let minX = Infinity; let minY = Infinity; let maxX = -Infinity; let maxY = -Infinity;
    const walk = (coords) => {
      if (typeof coords[0] === 'number') {
        minX = Math.min(minX, coords[0]);
        minY = Math.min(minY, coords[1]);
        maxX = Math.max(maxX, coords[0]);
        maxY = Math.max(maxY, coords[1]);
      } else {
        coords.forEach(walk);
      }
    };
    walk(geojson.features[0].geometry.coordinates);
    return [[minX, minY], [maxX, maxY]];
  }

  function renderBuildingLayer(geojson) {
    if (!geojson?.features?.length) return;
    buildingState = { geojson };
    ensureSource('building', { type: 'geojson', data: geojson });
    ensureLayer({
      id: 'building-fill',
      type: mapbox3dEnabled ? 'fill-extrusion' : 'fill',
      source: 'building',
      paint: mapbox3dEnabled
        ? {
          'fill-extrusion-color': '#22c55e',
          'fill-extrusion-height': 18,
          'fill-extrusion-opacity': 0.7,
        }
        : { 'fill-color': '#22c55e', 'fill-opacity': 0.35 },
    });
    if (!mapbox3dEnabled) {
      ensureLayer({
        id: 'building-line',
        type: 'line',
        source: 'building',
        paint: { 'line-color': '#7dd3fc', 'line-width': 2.5 },
      });
    }
    map.getSource('building')?.setData(geojson);
    map.fitBounds(boundsFromGeojson(geojson), { padding: 40, duration: 700 });
  }

  function addRasterOverlay(id, urlTemplate, opacity = 0.7, beforeId = null) {
    const sourceId = `ov-src-${id}`;
    const layerId = `ov-${id}`;
    overlays = overlays.filter((o) => o.id !== id);
    overlays.push({ id, urlTemplate, opacity, beforeId });
    if (!map.getSource(sourceId)) {
      map.addSource(sourceId, { type: 'raster', tiles: [urlTemplate], tileSize: 256, maxzoom: 19 });
    }
    if (!map.getLayer(layerId)) {
      const layer = { id: layerId, type: 'raster', source: sourceId, paint: { 'raster-opacity': opacity } };
      if (beforeId && map.getLayer(beforeId)) map.addLayer(layer, beforeId);
      else map.addLayer(layer);
    } else {
      map.setPaintProperty(layerId, 'raster-opacity', opacity);
    }
  }

  function removeRasterOverlay(id) {
    overlays = overlays.filter((o) => o.id !== id);
    const sourceId = `ov-src-${id}`;
    const layerId = `ov-${id}`;
    if (map.getLayer(layerId)) map.removeLayer(layerId);
    if (map.getSource(sourceId)) map.removeSource(sourceId);
  }

  function setOverlayOpacity(opacity) {
    overlays = overlays.map((o) => ({ ...o, opacity }));
    overlays.forEach((o) => {
      const layerId = `ov-${o.id}`;
      if (map.getLayer(layerId)) map.setPaintProperty(layerId, 'raster-opacity', opacity);
    });
  }

  function onMapClick(handler) {
    clickCb = (e) => handler({ latlng: { lat: e.lngLat.lat, lng: e.lngLat.lng } });
    attachClickDispatcher();
  }

  function onSavedPointClick(handler) {
    savedPointCb = handler;
    attachClickDispatcher();
  }

  function attachClickDispatcher() {
    // Remove any previous dispatcher so we don't stack listeners when switching styles.
    if (map.__pvClickDispatcher) map.off('click', map.__pvClickDispatcher);
    map.__pvClickDispatcher = (e) => {
      // If the user clicked a saved marker, consume the click.
      try {
        if (savedPointCb && map.getLayer('saved-points-circle')) {
          const feats = map.queryRenderedFeatures(e.point, { layers: ['saved-points-circle'] }) || [];
          const f = feats[0];
          if (f?.properties?.id) {
            savedPointCb({ id: String(f.properties.id) });
            return;
          }
        }
      } catch {
        // ignore
      }
      if (clickCb) clickCb(e);
    };
    map.on('click', map.__pvClickDispatcher);
  }

  function renderSavedPoints(points, activeId = null) {
    savedPointsState = { points: points || [], activeId };
    ensureSavedPointsLayer();
    const features = (points || []).map((p, idx) => ({
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [p.lon, p.lat] },
      properties: {
        id: String(p.id),
        label: String(p.label ?? (idx + 1)),
      },
    }));
    map.getSource('saved-points')?.setData({ type: 'FeatureCollection', features });
    try {
      map.setPaintProperty(
        'saved-points-circle',
        'circle-color',
        [
          'case',
          ['==', ['get', 'id'], activeId ?? ''],
          '#ffb347',
          '#2563eb',
        ],
      );
    } catch {
      // ignore
    }
  }

  function enableMapbox3D(token) {
    if (!token || typeof mapboxgl === 'undefined') return false;
    try {
      mapboxgl.accessToken = token;
      const center = map.getCenter();
      const zoom = map.getZoom();
      const bearing = map.getBearing();
      const pitch = map.getPitch();
      map.remove();

      const mb = new mapboxgl.Map({
        container: 'map',
        style: 'mapbox://styles/mapbox/dark-v11',
        center: [center.lng, center.lat],
        zoom,
        bearing,
        pitch: pitch || 50,
        antialias: true,
      });
      mb.addControl(new mapboxgl.NavigationControl({ visualizePitch: true }), 'top-left');
      mb.on('style.load', () => {
        const layers = mb.getStyle()?.layers ?? [];
        const labelLayerId = layers.find((l) => l.type === 'symbol' && l.layout?.['text-field'])?.id;
        mb.addLayer(
          {
            id: '3d-buildings',
            source: 'composite',
            'source-layer': 'building',
            filter: ['==', 'extrude', 'true'],
            type: 'fill-extrusion',
            minzoom: 14,
            paint: {
              'fill-extrusion-color': [
                'interpolate', ['linear'], ['get', 'height'],
                0, '#1a2540',
                50, '#26365e',
                150, '#3a5a8c',
                300, '#f59e0b',
              ],
              'fill-extrusion-height': ['get', 'height'],
              'fill-extrusion-base': ['get', 'min_height'],
              'fill-extrusion-opacity': 0.8,
            },
          },
          labelLayerId,
        );

        map = mb;
        mapbox3dEnabled = true;
        if (clickCb) map.on('click', clickCb);
        if (aoiState) drawAOI(aoiState.lat, aoiState.lon, aoiState.halfSizeDeg, aoiState.irradiance);
        if (buildingState) renderBuildingLayer(buildingState.geojson);
        overlays.forEach((ov) => addRasterOverlay(ov.id, ov.urlTemplate, ov.opacity, ov.beforeId));
      });
      return mb;
    } catch {
      return false;
    }
  }

  return {
    map,
    setBasemap,
    drawAOI,
    setAoiIntensity,
    renderBuildingLayer,
    renderSavedPoints,
    addRasterOverlay,
    removeRasterOverlay,
    setOverlayOpacity,
    onMapClick,
    onSavedPointClick,
    enableMapbox3D,
  };
}
