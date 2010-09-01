from sys import argv, stderr
from osgeo import ogr
from rtree import Rtree
from shapely.geos import lgeos
from shapely.geometry import MultiLineString, LineString, Polygon
from shapely.geometry.base import geom_factory
from shapely.wkb import loads, dumps
from shapely.ops import polygonize
from itertools import combinations

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

def load_datasource(filename):
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
    """
    """
    if shape.type != 'MultiLineString':
        return shape
    
    # copied from shapely.ops.linemerge at http://github.com/sgillies/shapely
    result = lgeos.GEOSLineMerge(shape._geom)
    return geom_factory(result)

def simplify(original_shape, tolerance, cross_check):
    """
    """
    if original_shape.type != 'LineString':
        return original_shape
    
    coords = list(original_shape.coords)
    new_coords = coords[:]
    
    if len(coords) <= 2:
        # don't shorten the too-short
        return original_shape
    
    # For each coordinate that forms the apex of a three-coordinate
    # triangle, find the area of that triangle and put it into a list
    # along with the coordinate index and the resulting line if the
    # triangle were flattened, ordered from smallest to largest.

    triples = [(i + 1, coords[i], coords[i + 1], coords[i + 2]) for i in range(len(coords) - 2)]
    triangles = [(i, Polygon([c1, c2, c3, c1]), c1, c3) for (i, c1, c2, c3) in triples]
    areas = sorted( [(triangle.area, i, c1, c3) for (i, triangle, c1, c3) in triangles] )

    min_area = tolerance ** 2
    
    if areas[0][0] > min_area:
        # there's nothing to be done
        return original_shape
    
    if cross_check:
        rtree = Rtree()
    
        # We check for intersections by building up an R-Tree index of each
        # and every line segment that makes up the original shape, and then
        # quickly doing collision checks against these.
    
        for j in range(len(coords) - 1):
            (x1, y1), (x2, y2) = coords[j], coords[j + 1]
            rtree.add(j, (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))
    
    preserved, popped = set(), False
    
    # Remove any coordinate that makes a triangle whose area is
    # below the minimum threshold, starting with the smallest and
    # working up. Mark points to be preserved until the recursive
    # call to simplify().
    
    for (area, index, ca, cb) in areas:
        if area > min_area:
            # there won't be any more points to remove.
            break
    
        if index in preserved:
            # the current point is too close to a previously-preserved one.
            continue
        
        preserved.add(index + 1)
        preserved.add(index - 1)

        if cross_check:
        
            # This is potentially a very expensive check, so we use the R-Tree
            # index we made earlier to rapidly cut down on the number of lines
            # from the original shape to check for collisions.
        
            (x1, y1), (x2, y2) = ca, cb
            new_line = LineString([ca, cb])

            box_ids = rtree.intersection((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))
            old_lines = [LineString(coords[j:j+2]) for j in box_ids]
            
            # Will removing this point result in an invalid geometry?

            if True in [old_line.crosses(new_line) for old_line in old_lines]:
                # Yes, because the index told us so.
                continue

            if new_line.crosses(original_shape):
                # Yes, because we painstakingly checked against the original shape.
                continue
        
        # It's safe to remove this point
        new_coords[index], popped = None, True
    
    new_coords = [coord for coord in new_coords if coord is not None]
    
    if cross_check:
        print 'simplify', len(coords), 'to', len(new_coords)
    
    if not popped:
        return original_shape
    
    return simplify(LineString(new_coords), tolerance, cross_check)

print >> stderr, 'Loading data...'

datasource = load_datasource(argv[1])
indexes = range(len(datasource.values))

print >> stderr, 'Making shared borders...'

graph, shared = {}, [[] for i in indexes]
comparison, comparisons = 0, len(indexes)**2 / 2

for (i, j) in combinations(indexes, 2):

    shape1 = datasource.shapes[i]
    shape2 = datasource.shapes[j]
    
    if shape1.intersects(shape2):
        print >> stderr, '%.2f%% -' % (100. * comparison/comparisons),
        print >> stderr, 'feature #%d and #%d' % (i, j),
        
        border = linemerge(shape1.intersection(shape2))

        graph[(i, j)] = True
        shared[i].append(border)
        shared[j].append(border)
        
        print >> stderr, '-', border.type

    comparison += 1

print >> stderr, 'Making unshared borders...'

unshared = []

for i in indexes:

    boundary = datasource.shapes[i].boundary
    
    for border in shared[i]:
        boundary = boundary.difference(border)

    unshared.append(boundary)

print >> stderr, 'Checking lengths...'

for i in indexes:

    shared_lengths = [border.length for border in shared[i]]
    
    tolerance, error = 0.000001, abs(datasource.shapes[i].length - unshared[i].length - sum(shared_lengths))
    assert error < tolerance, 'Feature #%(i)d error too large: %(error).8f > %(tolerance).8f' % locals()

print >> stderr, 'Building output...'

err_driver = ogr.GetDriverByName('ESRI Shapefile')
err_source = err_driver.CreateDataSource('err.shp')
assert err_source is not None, 'Failed creation of err.shp'
err_layer = err_source.CreateLayer('default', datasource.srs, ogr.wkbMultiLineString)

out_driver = ogr.GetDriverByName('ESRI Shapefile')
out_source = out_driver.CreateDataSource('out.shp')
assert out_source is not None, 'Failed creation of out.shp'
out_layer = out_source.CreateLayer('default', datasource.srs, ogr.wkbMultiPolygon)

for field in datasource.fields:
    for a_layer in (out_layer, err_layer):
        field_defn = ogr.FieldDefn(field.name, field.type)
        field_defn.SetWidth(field.width)
        a_layer.CreateField(field_defn)

tolerance = 650

for i in indexes:

    # Build up a list of linestrings that we will attempt to polygonize.

    parts = shared[i] + [unshared[i]]
    lines = []
    
    for part in parts:
        for geom in getattr(part, 'geoms', None) or [part]:
            if geom.type == 'LineString':
                lines.append(geom)

    try:
        # Try simplify without cross-checks because it's cheap and fast.
        simple_lines = [simplify(line, tolerance, False) for line in lines]
        poly = polygonize(simple_lines).next()

    except StopIteration:
        # A polygon wasn't found, for one of two reasons we're interested in:
        # the shape would be too small to show up with the given tolerance, or
        # the simplification resulted in an invalid, self-intersecting shape.
        
        lost_area = datasource.shapes[i].area
        lost_portion = lost_area / (tolerance ** 2)
        
        if lost_portion < 4:
            # It's just small.
            print >> stderr, 'Skipped small feature #%(i)d' % locals()
            continue

        # A large lost_portion is a warning sign that we have an invalid polygon.
        
        try:
            # Try simplify again with cross-checks because it's slow but careful.
            simple_lines = [simplify(line, tolerance, True) for line in lines]
            poly = polygonize(simple_lines).next()

        except StopIteration:
            # Again no polygon was found, which now probably means we have
            # an actual error that should be saved to the error output file.
    
            #raise Warning('Lost feature #%(i)d, %(lost_portion)d times larger than maximum tolerance' % locals())
            print >> stderr, 'Lost feature #%(i)d, %(lost_portion)d times larger than maximum tolerance' % locals()
    
            feat = ogr.Feature(err_layer.GetLayerDefn())
            
            for (j, field) in enumerate(datasource.fields):
                feat.SetField(field.name, datasource.values[i][j])
            
            multiline = MultiLineString([list(line.coords) for line in lines])
            
            geom = ogr.CreateGeometryFromWkb(dumps(multiline))
            
            feat.SetGeometry(geom)
        
            err_layer.CreateFeature(feat)

            continue
        
    #
    
    feat = ogr.Feature(out_layer.GetLayerDefn())
    
    for (j, field) in enumerate(datasource.fields):
        feat.SetField(field.name, datasource.values[i][j])
    
    geom = ogr.CreateGeometryFromWkb(dumps(poly))
    
    feat.SetGeometry(geom)

    out_layer.CreateFeature(feat)
