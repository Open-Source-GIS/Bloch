from sys import argv, stderr, exit
from os.path import splitext
from optparse import OptionParser
from itertools import combinations, permutations
from sqlite3 import connect

from osgeo import ogr
from rtree import Rtree
from rtree.core import RTreeError
from shapely.geos import lgeos
from shapely.geometry import MultiLineString, LineString, Polygon, Point
from shapely.geometry.base import geom_factory
from shapely.wkb import loads, dumps
from shapely.ops import polygonize

drivers = {'.shp': 'ESRI Shapefile', '.json': 'GeoJSON'}

class Field:
    """
    """
    def __init__(self, name, type, width):
        self.name = name
        self.type = type
        self.width = width

class Datasource:
    """
    """
    def __init__(self, srs, geom_type, fields, values, shapes):
        self.srs = srs
        self.fields = fields
        self.geom_type = geom_type
        self.values = values
        self.shapes = shapes
        
        # guid, src1_id, src2_id, line_id, x1, y1, x2, y2
        
        db = connect(':memory:').cursor()
        
        db.execute("""CREATE table segments (
                        
                        -- global identifier for this segment
                        guid    INTEGER PRIMARY KEY AUTOINCREMENT,
        
                        -- identifiers for source shape or shapes for shared borders
                        src1_id INTEGER,
                        src2_id INTEGER,
                        
                        -- global identifier for this line
                        line_id INTEGER,
                        
                        -- start and end coordinates for this segment
                        x1      REAL,
                        y1      REAL,
                        x2      REAL,
                        y2      REAL,
                        
                        -- flag
                        removed INTEGER
        
                      )""")
        
        db.execute('CREATE INDEX segments_lines ON segments (line_id, guid)')
        db.execute('CREATE INDEX shape1_parts ON segments (src1_id)')
        db.execute('CREATE INDEX shape2_parts ON segments (src2_id)')
        
        self.db = db
        self.rtree = Rtree()
        self.memo_line = make_memo_line()

    def indexes(self):
        return range(len(self.values))

def load(filename):
    """
    """
    print >> stderr, 'Making data source...'
    datasource = make_datasource(filename)
    
    print >> stderr, 'Making shared borders...'
    shared_borders = populate_shared_segments_by_combination(datasource)
    
    print >> stderr, 'Making unshared borders...'
    populate_unshared_segments(datasource, shared_borders)
    
    return datasource

def make_datasource(filename):
    """
    """
    source = ogr.Open(filename)

    layer = source.GetLayer(0)
    srs = layer.GetSpatialRef()
    layer_defn = layer.GetLayerDefn()
    geom_type = layer_defn.GetGeomType()
    
    fields = [Field(field_defn.GetNameRef(), field_defn.GetType(), field_defn.GetWidth())
              for field_defn 
              in [layer_defn.GetFieldDefn(i) for i in range(layer_defn.GetFieldCount())]]

    values, shapes = [], []
    
    for feature in layer:
        values.append([feature.GetField(field.name) for field in fields])
        shapes.append(loads(feature.geometry().ExportToWkb()))

    return Datasource(srs, geom_type, fields, values, shapes)

def linemerge(shape):
    """ Returns a geometry with lines merged using GEOSLineMerge.
    """
    if shape.type != 'MultiLineString':
        return shape
    
    # copied from shapely.ops.linemerge at http://github.com/sgillies/shapely
    result = lgeos.GEOSLineMerge(shape._geom)
    return geom_factory(result)

def populate_shared_segments_by_combination(datasource):
    """
    """
    shared = [[] for i in datasource.indexes()]
    comparison, comparisons = 0, len(datasource.indexes())**2 / 2
    
    for (i, j) in combinations(datasource.indexes(), 2):
    
        shape1 = datasource.shapes[i]
        shape2 = datasource.shapes[j]
        
        if shape1.intersects(shape2):
            print >> stderr, '%.2f%% -' % (100. * comparison/comparisons),
            print >> stderr, 'features %d and %d:' % (i, j),
            
            border = linemerge(shape1.intersection(shape2))
            
            geoms = hasattr(border, 'geoms') and border.geoms or [border]
            
            for geom in geoms:
                try:
                    line_id = datasource.rtree.count(datasource.rtree.get_bounds())
                except RTreeError:
                    line_id = 0
        
                coords = list(geom.coords)
                segments = [coords[k:k+2] for k in range(len(coords) - 1)]
                
                for ((x1, y1), (x2, y2)) in segments:
                    datasource.db.execute("""INSERT INTO segments
                                             (src1_id, src2_id, line_id, x1, y1, x2, y2, removed)
                                             VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
                                          (i, j, line_id, x1, y1, x2, y2))
                    
                    bbox = min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)
                    datasource.rtree.add(datasource.db.lastrowid, bbox)

                print >> stderr, len(coords), '-',
            
            shared[i].append(border)
            shared[j].append(border)
            
            print >> stderr, border.type
    
        comparison += 1

    return shared

def populate_shared_segments_by_rtree(datasource):
    """
    """
    rtree = Rtree()
    indexes = datasource.indexes()
    
    for i in indexes:
        xmin, ymin, xmax, ymax = datasource.shapes[i].bounds
        
        xbuf = (xmax - xmin) * .001
        ybuf = (ymax - ymin) * .001
        
        bounds = (xmin-xbuf, ymin-ybuf, xmax+xbuf, ymax+ybuf)
        
        rtree.add(i, bounds)
    
    shared = [[] for i in indexes]
    
    for i in indexes:
        for j in rtree.intersection(datasource.shapes[i].bounds):
            
            if i >= j:
                continue
            
            shape1 = datasource.shapes[i]
            shape2 = datasource.shapes[j]
            
            if not shape1.intersects(shape2):
                continue
            
            print >> stderr, 'Features %d and %d:' % (i, j), 'of', len(indexes),
            
            border = linemerge(shape1.intersection(shape2))
            
            geoms = hasattr(border, 'geoms') and border.geoms or [border]
            
            for geom in geoms:
                try:
                    line_id = datasource.rtree.count(datasource.rtree.get_bounds())
                except RTreeError:
                    line_id = 0
        
                coords = list(geom.coords)
                segments = [coords[k:k+2] for k in range(len(coords) - 1)]
                
                for ((x1, y1), (x2, y2)) in segments:
                    datasource.db.execute("""INSERT INTO segments
                                             (src1_id, src2_id, line_id, x1, y1, x2, y2, removed)
                                             VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
                                          (i, j, line_id, x1, y1, x2, y2))
                    
                    bbox = min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)
                    datasource.rtree.add(datasource.db.lastrowid, bbox)

                print >> stderr, len(coords), '-',
            
            shared[i].append(border)
            shared[j].append(border)
            
            print >> stderr, border.type

    return shared

def populate_unshared_segments(datasource, shared):
    """
    """
    for i in datasource.indexes():
    
        boundary = datasource.shapes[i].boundary
        
        for border in shared[i]:
            boundary = boundary.difference(border)
        
        print >> stderr, 'Feature %d:' % i,
    
        geoms = hasattr(boundary, 'geoms') and boundary.geoms or [boundary]
        geoms = [geom for geom in geoms if hasattr(geom, 'coords')]
        
        for geom in geoms:
            try:
                line_id = datasource.rtree.count(datasource.rtree.get_bounds())
            except RTreeError:
                line_id = 0
    
            coords = list(geom.coords)
            segments = [coords[k:k+2] for k in range(len(coords) - 1)]
            
            for ((x1, y1), (x2, y2)) in segments:
                datasource.db.execute("""INSERT INTO segments
                                         (src1_id, line_id, x1, y1, x2, y2, removed)
                                         VALUES (?, ?, ?, ?, ?, ?, 0)""",
                                      (i, line_id, x1, y1, x2, y2))
                
                bbox = min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)
                datasource.rtree.add(datasource.db.lastrowid, bbox)
    
            print >> stderr, len(coords), '-',
    
        print >> stderr, boundary.type

def simplify_linework(datasource, tolerance):
    """ Do the thing.
    """
    q = 'SELECT line_id, COUNT(guid) AS guids FROM segments WHERE removed=0 GROUP BY line_id order by guids DESC'
    line_ids = [line_id for (line_id, count) in datasource.db.execute(q)]
    
    stable_lines = set()
    
    while True:
    
        was = datasource.db.execute('SELECT COUNT(*) FROM segments WHERE removed=0').fetchone()[0]
        
        preserved, popped = set(), False
        
        for line_id in line_ids:
        
            if line_id in stable_lines:
                continue
            
            # For each coordinate that forms the apex of a two-segment
            # triangle, find the area of that triangle and put it into a list
            # along with the segment identifier and the resulting line if the
            # triangle were flattened, ordered from smallest to largest.
        
            rows = datasource.db.execute("""SELECT guid, x1, y1, x2, y2
                                            FROM segments
                                            WHERE line_id = ?
                                              AND removed = 0
                                            ORDER BY guid""",
                                         (line_id, ))
            
            segs = [(guid, (x1, y1), (x2, y2)) for (guid, x1, y1, x2, y2) in rows]
            triples = [(segs[i][0], segs[i+1][0], segs[i][1], segs[i][2], segs[i+1][2]) for i in range(len(segs) - 1)]
            triangles = [(guid1, guid2, Polygon([c1, c2, c3, c1]), c1, c3) for (guid1, guid2, c1, c2, c3) in triples]
            areas = sorted( [(triangle.area, guid1, guid2, c1, c3) for (guid1, guid2, triangle, c1, c3) in triangles] )
            
            min_area = tolerance ** 2
            
            if not areas or areas[0][0] > min_area:
                # there's nothing to be done
                stable_lines.add(line_id)
                stderr.write('-')
                continue
            
            # Reduce any segments that makes a triangle whose area is below
            # the minimum threshold, starting with the smallest and working up.
            # Mark segments to be preserved until the next iteration.
            
            for (area, guid1, guid2, ca, cb) in areas:
                if area > min_area:
                    # there won't be any more points to remove.
                    break
                
                if guid1 in preserved or guid2 in preserved:
                    # the current segment is too close to a previously-preserved one.
                    continue
        
                # Check the resulting flattened line against the rest
                # any of the original shapefile, to determine if it would
                # cross any existing line segment.
                
                (x1, y1), (x2, y2) = ca, cb
                new_line = datasource.memo_line(x1, y1, x2, y2)
        
                old_guids = datasource.rtree.intersection(bbox(x1, y1, x2, y2))
                old_rows = datasource.db.execute('SELECT x1, y1, x2, y2 FROM segments WHERE guid IN (%s) AND removed=0' % ','.join(map(str, old_guids)))
                old_lines = [datasource.memo_line(x1, y1, x2, y2) for (x1, y1, x2, y2) in old_rows]
                
                if True in [new_line.crosses(old_line) for old_line in old_lines]:
                    stderr.write('x%d' % line_id)
                    continue
                
                preserved.add(guid1)
                preserved.add(guid2)
                
                popped = True
                
                x1, y1, x2, y2 = ca[0], ca[1], cb[0], cb[1]
        
                datasource.db.execute('UPDATE segments SET removed=1 WHERE guid=%d' % guid2)
                datasource.db.execute('UPDATE segments SET x1=?, y1=?, x2=?, y2=? WHERE guid=?',
                                      (x1, y1, x2, y2, guid1))
        
                datasource.rtree.add(guid1, bbox(x1, y1, x2, y2))
            
            stderr.write('.')
        
        print >> stderr, ' reduced from', was, 'to',
        print >> stderr, datasource.db.execute('SELECT COUNT(guid) FROM segments WHERE removed=0').fetchone()[0],
        
        datasource.rtree = Rtree()
        
        for (guid, x1, y1, x2, y2) in datasource.db.execute('SELECT guid, x1, y1, x2, y2 FROM segments WHERE removed=0'):
            datasource.rtree.add(guid1, bbox(x1, y1, x2, y2))
            
        print >> stderr, '.'

        if not popped:
            break

def save_datasource(datasource, filename):
    """
    """
    ext = splitext(filename)[1]
    
    out_driver = ogr.GetDriverByName(drivers.get(ext))
    out_source = out_driver.CreateDataSource(filename)
    assert out_source is not None, 'Failed creation of %s' % filename
    out_layer = out_source.CreateLayer('default', datasource.srs, ogr.wkbMultiPolygon)
    
    for field in datasource.fields:
        field_defn = ogr.FieldDefn(field.name, field.type)
        field_defn.SetWidth(field.width)
        out_layer.CreateField(field_defn)
    
    for i in datasource.indexes():
    
        segments = datasource.db.execute("""SELECT x1, y1, x2, y2
                                            FROM segments
                                            WHERE (src1_id = ? OR src2_id = ?)
                                              AND removed = 0""", (i, i))
    
        lines = [datasource.memo_line(x1, y1, x2, y2) for (x1, y1, x2, y2) in segments]
        
        try:
            poly = polygonize(lines).next()
    
        except StopIteration:
            lost_area = datasource.shapes[i].area
            lost_portion = lost_area / (tolerance ** 2)
            
            if lost_portion < 4:
                # It's just small.
                print >> stderr, 'Skipped small feature #%(i)d' % locals()
                continue
    
            # This is a bug we don't understand yet.
            raise Exception('Failed to get a meaningful polygon out of large feature #%(i)d' % locals())
        
        feat = ogr.Feature(out_layer.GetLayerDefn())
        
        for (j, field) in enumerate(datasource.fields):
            feat.SetField(field.name, datasource.values[i][j])
        
        geom = ogr.CreateGeometryFromWkb(dumps(poly))
        
        feat.SetGeometry(geom)
    
        out_layer.CreateFeature(feat)

def bbox(x1, y1, x2, y2):
    return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)

def make_memo_line():
    """ Return a function that memorizes line strings to save on construction costs.
    """
    line_memory = {}
    
    def memo_line(x1, y1, x2, y2):
        key = (x1, y1, x2, y2)

        if key not in line_memory:
            line_memory[key] = LineString([(x1, y1), (x2, y2)])

        return line_memory[key]

    return memo_line

parser = OptionParser(usage="""%prog <input file> <tolerance> <output file> [<tolerance> <output file>]+

..""")

if __name__ == '__main__':
    opts, args = parser.parse_args()
    
    infile, outargs = args[0], args[1:]
    outfiles = [(int(outargs[i]), outargs[i + 1]) for i in range(0, len(outargs), 2)]
    
    print >> stderr, 'Loading data...'
    datasource = load(infile)
    
    print >> stderr, len(datasource.indexes()), 'shapes,',
    print >> stderr, datasource.db.execute('SELECT COUNT(DISTINCT line_id) FROM segments').fetchone()[0], 'lines,',
    print >> stderr, datasource.db.execute('SELECT COUNT(DISTINCT guid) FROM segments').fetchone()[0], 'segments.'
    
    for (tolerance, outfile) in sorted(outfiles):
    
        print >> stderr, 'Simplifying linework to %d...' % tolerance
        simplify_linework(datasource, tolerance)
        
        print >> stderr, 'Building %s...' % outfile
        save_datasource(datasource, outfile)
