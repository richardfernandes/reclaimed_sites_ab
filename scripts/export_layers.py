# Imports
import geopandas as gpd
import os
import sys
import janitor
import ee
import json
import math
import time
from shapely.geometry import Polygon, MultiPolygon

def simplify_geometry(geom, tolerance=0.1):
    if geom.geom_type in ['Polygon', 'MultiPolygon']:
        return geom.simplify(tolerance, preserve_topology=True)
    return geom

def count_points(geom):
    if isinstance(geom, Polygon):
        return len(geom.exterior.coords) + sum(len(inner.coords) for inner in geom.interiors)
    elif isinstance(geom, MultiPolygon):
        return sum(count_points(poly) for poly in geom.geoms)
    return 0

def assets_exists(asset_id):
    """Validate if asset exists in GEE"""
    try:
        ee.data.getAsset(asset_id)
        return True
    except Exception:
        return False
    
def remove_z(geometry):
    if geometry.has_z:
        if isinstance(geometry, Polygon):
            return Polygon([(x, y) for x, y, z in geometry.exterior.coords], [([(x, y) for x, y, z in interior.coords]) for interior in geometry.interiors])
        elif isinstance(geometry, MultiPolygon):
            return MultiPolygon([Polygon([(x, y) for x, y, z in poly.exterior.coords], [([(x, y) for x, y, z in interior.coords]) for interior in poly.interiors]) for poly in geometry.geoms])
    return geometry

def check_empty_coordinates(feature):
    """Flag empty geometries based on the coordinates of the geometry."""
    coordinates = feature.geometry().coordinates()
    is_empty = coordinates.size().eq(0)
    return feature.set('empty_buffer', is_empty)

def wait_for_tasks(task_list):
    while True:
        tasks_completed = all(task.status()['state'] in ['COMPLETED', 'FAILED', 'CANCELLED'] for task in task_list)
        if tasks_completed:
            break
        time.sleep(10)  

    # Check for any failed tasks
    failed_tasks = [task for task in task_list if task.status()['state'] == 'FAILED']
    if failed_tasks:
        raise Exception(f"The following tasks failed: {', '.join(task.status()['description'] for task in failed_tasks)}")

def merge_collections(asset_ids, layer_name):
    print(f"Merging collections for {layer_name}...")
    merged_fc = None
    batch_size = 100  

    for i in range(0, len(asset_ids), batch_size):
        batch_ids = asset_ids[i:i+batch_size]
        batch_fc = ee.FeatureCollection(batch_ids[0])
        for asset_id in batch_ids[1:]:
            batch_fc = batch_fc.merge(ee.FeatureCollection(asset_id))
        
        if merged_fc is None:
            merged_fc = batch_fc
        else:
            merged_fc = merged_fc.merge(batch_fc)
        
        print(f"Merged batch {i//batch_size + 1} of {math.ceil(len(asset_ids)/batch_size)}")
    
    return merged_fc

def process_layer(layer_name, layer_data):
    print(f"Processing {layer_name}...")

    data = layer_data
    print(f'Total number of {layer_name}: {len(data)}')
    print(f'Original data CRS: {data.crs}')

    batch_size = 1

    num_batches = math.ceil(len(data) / batch_size)
    print(f'Total of batches to export: {num_batches}')

    export_tasks = []

    for i in range(num_batches):
        start_idx = i * batch_size
        end_idx = min((i + 1) * batch_size, len(data))
        
        batch = data.iloc[start_idx:end_idx].to_crs(epsg=4326)
        batch_geojson = batch.to_json()
        print(f'Transforming to json batch {i}/{num_batches}')
        batch_fc = ee.FeatureCollection(json.loads(batch_geojson))

    # for i, batch in enumerate(data.groupby(data.index // batch_size)):
    #     batch = batch[1].to_crs(epsg=4326)
    #     batch_geojson = batch.to_json()
    #     print(f'Transforming to json batch {i}')
    #     batch_fc = ee.FeatureCollection(json.loads(batch_geojson))

        batch_asset_id = f'projects/ee-ronnyale/assets/{layer_name}_batch_{i+1}'

        if not assets_exists(batch_asset_id):
            print(f'Exporting the batch: {batch_asset_id}')
            exportTask = ee.batch.Export.table.toAsset(
                collection=batch_fc,
                description=f'{layer_name.capitalize()} Batch {i+1}',
                assetId=batch_asset_id
                )
            exportTask.start()
            export_tasks.append(exportTask)
            print(f'Export task for {batch_asset_id} started')
        else:
            print(f'Export skipped: Asset already exists at {batch_asset_id}')

    print(f"Waiting for all {layer_name} batch export tasks to complete...")
    wait_for_tasks(export_tasks)
    print(f"All {layer_name} batch export tasks completed successfully.")

    batch_asset_ids = [f'projects/ee-ronnyale/assets/{layer_name}_batch_{i+1}' for i in range(num_batches)]
    print(batch_asset_ids)
    print(f'Merging {layer_name} batches...')
    merged_fc = merge_collections(batch_asset_ids, layer_name)
    merged_fc_flag = merged_fc.map(check_empty_coordinates)
    merged_fc_complete = merged_fc_flag.filter(ee.Filter.eq('empty_buffer', 0))

    print(f'Done merging {layer_name} batches...')
    print(f'Total number of features: {merged_fc.size().getInfo()}')

    exportTask = ee.batch.Export.table.toAsset(
        collection=merged_fc_complete,
        description=f'Merged {layer_name.capitalize()}',
        assetId=f'projects/ee-ronnyale/assets/{layer_name}_merged'
    )
    print(f'Exporting merged {layer_name} asset')
    exportTask.start()

    print(f"Waiting for merged {layer_name} asset export to complete...")
    wait_for_tasks([exportTask])
    print(f"Merged {layer_name} asset export completed successfully.")

    print(f'Deleting individual {layer_name} batch assets...')
    for asset_id in batch_asset_ids:
        try:
            ee.data.deleteAsset(asset_id)
            print(f'Successfully deleted: {asset_id}')
        except Exception as e:
            print(f'Failed to delete {asset_id}: {e}')

    print(f"Finished processing {layer_name}.")


# Main execution
if __name__ == "__main__":
    ee.Initialize()

    # Read data
    sys.path.append(os.path.abspath(os.path.join('..')))

    # reservoirs = gpd.read_file('data_check/HFI2021.gdb',
    #                                 layer = 'o01_Reservoirs_HFI_2021')
    # reservoirs = reservoirs.clean_names()
    # reservoirs = reservoirs[['feature_ty', 'geometry']]

    # roads = gpd.read_file('data_check/HFI2021.gdb',
    #                                 layer = 'o03_Roads_HFI_2021')
    # roads = roads.clean_names()
    # roads = roads[['feature_ty', 'geometry']]

    # residentials = gpd.read_file('data_check/HFI2021.gdb',
    #                                 layer = 'o15_Residentials_HFI_2021')
    # residentials = residentials.clean_names()
    # residentials = residentials[['feature_ty', 'geometry']]

    # industrials = gpd.read_file('data_check/HFI2021.gdb',
    #                                 layer = 'o08_Industrials_HFI_2021')
    # industrials = industrials.clean_names()
    # industrials = industrials[['feature_ty', 'geometry']]
    # industrials = industrials.dropna(subset=['geometry'])
    
    
    # fires = gpd.read_file('data_check/NFDB_poly_20210707.shp')
    # Option to simplify fires layer
    fires = gpd.read_file('data_check/NFDB_poly_20210707.shp', 
                      engine='fiona', 
                      parse_geometry_opts={'METHOD': 'SKIP'})
    fires = fires.clean_names()
    fires = fires[fires['src_agency'] == "AB"]
    fires = fires[['fire_id', 'rep_date', 'geometry']]
    fires['geometry'] = fires['geometry'].apply(remove_z)
    fires['geometry'] = fires['geometry'].apply(lambda geom: simplify_geometry(geom, tolerance = 1.5))

    # Print statistics before simplification
    print(f"Number of geometries: {len(fires)}")
    print(f"Total number of points before simplification: {sum(fires.geometry.apply(count_points))}")

    # Apply geometry simplification
    fires['geometry'] = fires['geometry'].apply(lambda geom: simplify_geometry(geom))

    # Print statistics after simplification
    print(f"Total number of points after simplification: {sum(fires.geometry.apply(count_points))}")
    
    abandoned_wells = gpd.read_file('data_check/HFI2021.gdb',
                                layer = 'o16_WellsAbnd_HFI_2021')
    abandoned_wells = abandoned_wells.clean_names()

    # Map reclamation values
    abandoned_wells['reclamation_status'] = abandoned_wells['reclamation_status'].map({
        1: 'not_reclaimed',
        2: 'reclamation_exempt',
        3: 'reclaimed'
    }).fillna('no_data')

    # Filter polygons
    selected_polygons = (
            abandoned_wells
            .query("reclamation_status == 'reclaimed'")
            .query("reclamation_date != 0")
            .query("reclamation_date > max_abandoned_date")
            .query("reclamation_date > max_last_production_date")
            .query("max_abandoned_date > max_last_production_date")
            .drop(columns=['first_spud_date']) 
    )

    layers = [
        # ('reservoirs', reservoirs),
        # ('residentials', residentials),
        # ('roads', roads),
        # ('industrials', industrials),
        ('fires', fires)
        # ('abandoned_wells', selected_polygons)
    ]

    # Process each layer
    for layer_name, layer_data in layers:
        process_layer(layer_name, layer_data)

    print("All layers have been processed successfully.")



