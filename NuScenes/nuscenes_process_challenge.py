import sys
import os
import numpy as np
from scipy import spatial 
import pickle
import torch 
import torch.nn.functional as F
from nuscenes.nuscenes import NuScenes
from nuscenes.map_expansion.map_api import NuScenesMap
from nuscenes.eval.prediction.splits import get_prediction_challenge_split
from nuscenes.eval.prediction.config import load_prediction_config
from nuscenes.prediction import PredictHelper
from nuscenes.utils.splits import create_splits_scenes
import pandas as pd
from collections import defaultdict
from pyquaternion import Quaternion
from torchvision import transforms

from nuscenes.prediction.input_representation.static_layers import StaticLayerRasterizer
from nuscenes.prediction.input_representation.agents import AgentBoxesWithFadedHistory
from nuscenes.prediction.input_representation.interface import InputRepresentation
from nuscenes.prediction.input_representation.combinators import Rasterizer


#508 0 sequences???
scene_blacklist = [499, 515, 517]

max_num_objects = 50  #pkl np.arrays with same dimensions
total_feature_dimension = 16 #x,y,heading,vel[x,y],acc[x,y],head_rate, type, l,w,h, frame_id, scene_id, mask, num_visible_objects

FREQUENCY = 2
dt = 1 / FREQUENCY
history = 3
future = 3
history_frames = history*FREQUENCY
future_frames = future*FREQUENCY
total_frames = history_frames + future_frames + 1 #2s of history + 6s of prediction + FRAME ACTUAL

# This is the path where you stored your copy of the nuScenes dataset.
DATAROOT = '/media/14TBDISK/nuscenes'
nuscenes = NuScenes('v1.0-trainval', dataroot=DATAROOT)   #850 scenes
# Helper for querying past and future data for an agent.
helper = PredictHelper(nuscenes)
base_path = '/media/14TBDISK/sandra/nuscenes_processed'
base_path_map = os.path.join(base_path, 'hd_maps_challenge_224')

static_layer_rasterizer = StaticLayerRasterizer(helper)
agent_rasterizer = AgentBoxesWithFadedHistory(helper, seconds_of_history=history)
input_representation = InputRepresentation(static_layer_rasterizer, agent_rasterizer, Rasterizer())
transform = transforms.Compose(
                            [
                                #transforms.ToTensor(),
                                transforms.Resize((112,112), interpolation=3),
                                #transforms.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))
                            ]
                        )


neighbor_distance = 40

def pol2cart(th, r):
    """
    Transform polar to cartesian coordinates.
    :param th: Nx1 ndarray
    :param r: Nx1 ndarray
    :return: Nx2 ndarray
    """

    x = np.multiply(r, np.cos(th))
    y = np.multiply(r, np.sin(th))

    cart = np.array([x, y]).transpose()
    return cart

def cart2pol(cart):
    """
    Transform cartesian to polar coordinates.
    :param cart: Nx2 ndarray
    :return: 2 Nx1 ndarrays
    """
    if cart.shape == (2,):
        cart = np.array([cart])

    x = cart[:, 0]
    y = cart[:, 1]

    th = np.arctan2(y, x)
    r = np.sqrt(np.power(x, 2) + np.power(y, 2))
    return th, r

def calculate_rotated_bboxes(center_points_x, center_points_y, length, width, rotation=0):
    """
    Calculate bounding box vertices from centroid, width and length.
    :param centroid: center point of bbox
    :param length: length of bbox
    :param width: width of bbox
    :param rotation: rotation of main bbox axis (along length)
    :return:
    """

    centroid = np.array([center_points_x, center_points_y]).transpose()

    centroid = np.array(centroid)
    if centroid.shape == (2,):
        centroid = np.array([centroid])

    # Preallocate
    data_length = centroid.shape[0]
    rotated_bbox_vertices = np.empty((data_length, 4, 2))

    # Calculate rotated bounding box vertices
    rotated_bbox_vertices[:, 0, 0] = -length / 2
    rotated_bbox_vertices[:, 0, 1] = -width / 2

    rotated_bbox_vertices[:, 1, 0] = length / 2
    rotated_bbox_vertices[:, 1, 1] = -width / 2

    rotated_bbox_vertices[:, 2, 0] = length / 2
    rotated_bbox_vertices[:, 2, 1] = width / 2

    rotated_bbox_vertices[:, 3, 0] = -length / 2
    rotated_bbox_vertices[:, 3, 1] = width / 2

    for i in range(4):
        th, r = cart2pol(rotated_bbox_vertices[:, i, :])
        rotated_bbox_vertices[:, i, :] = pol2cart(th + rotation, r).squeeze()
        rotated_bbox_vertices[:, i, :] = rotated_bbox_vertices[:, i, :] + centroid

    return rotated_bbox_vertices


def process_tracks(tracks,  start_frame, end_frame, current_frame,sample_token, annotations):
    '''
        Tracks: a list of (n_frames ~40f = 20s) tracks_per_frame ordered by frame.
                Each row (track) contains a dict, where each key corresponds to an array of data from all agents in that frame.
        
        Returns data processed for a sequence of 8s (2s of history, 6s of labels)
    '''
    visible_node_id_list = tracks[current_frame]["node_id"][:-1]  #All agents in the current frame but ego (changes token in each frame)    

    current_anns = [instance + '_' + sample_token for instance in visible_node_id_list]
    challenge_anns = [ ann.split('_')[0] for ann in current_anns if ann in annotations ]
    
    num_visible_object = len(visible_node_id_list) + 1

    #Zero-centralization per frame (sequence)
    mean_xy = [tracks[current_frame]['x_global'].mean(),tracks[current_frame]['y_global'].mean(),0]
    #tracks['position'][:,:2] = tracks['position'][:,:2] - mean_xy

    # You can convert global coords to local frame with: helper.convert_global_coords_to_local(coords,starting_annotation['translation'], starting_annotation['rotation'])
    # x_global y_global are centralized in 0 taking into account all objects positions in the current frame
    xy = tracks[current_frame]['position'][:, :2].astype(float)
    # Compute distance between any pair of objects
    dist_xy = spatial.distance.cdist(xy, xy)  
    # If their distance is less than ATTENTION RADIUS (neighbor_distance), we regard them as neighbors.
    neighbor_matrix = np.zeros((max_num_objects, max_num_objects))
    neighbor_matrix[:num_visible_object,:num_visible_object] = (dist_xy<neighbor_distance).astype(int)

    '''
    ############# FIRST OPTION ############
    # Get past and future trajectories
    future_xy_local = np.zeros((num_visible_object, future_frames*2))
    past_xy_local = np.zeros((num_visible_object, 2*history_frames))
    mask = np.zeros((num_visible_object, future_frames))
    for i, node_id in enumerate(track['node_id']):
        future_xy_i=helper.get_future_for_agent(node_id,sample_token, seconds=future, in_agent_frame=True).reshape(-1)
        past_xy_i=helper.get_past_for_agent(node_id,sample_token, seconds=history, in_agent_frame=True).reshape(-1)
        past_xy_local[i,:len(past_xy_i)] = past_xy_i
        future_xy_local[i, :len(future_xy_i)] = future_xy_i # Some agents don't have 6s of future or 2s of history, pad with 0's
        mask[i,:len(future_xy_i)//2] += np.ones((len(future_xy_i)//2))
        
    object_features = np.column_stack((
            track['position'], track['motion'], past_xy_local, future_xy_local, mask, track['info_agent'],
            track['info_sequence'] ))  # 3 + 3 + 8 + 24 + 12 + 4 + 2 = 56   

    inst_sample_tokens = np.column_stack((track['node_id'], track['sample_token']))
    '''

    ############ SECOND OPTION ###############3
    object_feature_list = []
    while start_frame < 0:
        object_feature_list.append(np.zeros(shape=(num_visible_object,total_feature_dimension)))
        start_frame += 1
    '''
    if len(tracks)-1 >= end_frame:
        last_frame = len(tracks)
    else:
        last_frame = end_frame+1
    '''
    now_all_object_id = set([val for frame in range(start_frame, end_frame+1) for val in tracks[frame]["node_id"]])  #todos los obj en los15 hist frames

    for frame_ind in range(start_frame, end_frame + 1):	
        now_frame_feature_dict = {node_id : (
            list(tracks[frame_ind]['position'][np.where(np.array(tracks[frame_ind]['node_id'])==node_id)[0][0]] - mean_xy)+ 
            list(tracks[frame_ind]['motion'][np.where(np.array(tracks[frame_ind]['node_id'])==node_id)[0][0]]) + 
            list(tracks[frame_ind]['info_agent'][np.where(np.array(tracks[frame_ind]['node_id'])==node_id)[0][0]]) +
            list(tracks[frame_ind]['info_sequence'][0]) + 
            [1 if node_id in challenge_anns else 0] + [num_visible_object]  # mask=0 if inst_sample not in prediction_challenge.json 
            ) for node_id in tracks[frame_ind]["node_id"] if node_id in visible_node_id_list}
        # if the current object is not at this frame, we return all 0s 
        now_frame_feature = np.array([now_frame_feature_dict.get(vis_id, np.zeros(total_feature_dimension)) for vis_id in visible_node_id_list])
        
        ego_feature = np.array((list(tracks[frame_ind]['position'][-1] - mean_xy) + list(tracks[frame_ind]['motion'][-1]) + list(tracks[frame_ind]['info_agent'][-1]) + list(tracks[frame_ind]['info_sequence'][-1]) + [0] + [num_visible_object])).reshape(1, total_feature_dimension)
        now_frame_feature = np.vstack((now_frame_feature, ego_feature))
        object_feature_list.append(now_frame_feature)

    if end_frame-current_frame < future_frames:
        for i in range( future_frames-( end_frame-current_frame ) ):
            object_feature_list.append(np.zeros(shape=(num_visible_object,total_feature_dimension)))
    

    object_feature_list = np.array(object_feature_list)  # T,V,C
    assert object_feature_list.shape[1] < max_num_objects
    assert object_feature_list.shape[0] == total_frames
    object_frame_feature = np.zeros((max_num_objects, total_frames, total_feature_dimension))  # V, T, C
    object_frame_feature[:num_visible_object] = np.transpose(object_feature_list, (1,0,2))
    inst_sample_tokens = np.column_stack((tracks[current_frame]['node_id'], tracks[current_frame]['sample_token']))
    #visible_object_indexes = [list(now_all_object_id).index(i) for i in visible_node_id_list]
    return object_frame_feature, neighbor_matrix, mean_xy, inst_sample_tokens


def process_scene(scene, samples, instances, scene_annotations):
    '''
    Returns a list of (n_frames ~40f = 20s) tracks_per_frame ordered by frame.
    Each row contains a dict, where each key corresponds to an array of data from all agents in that frame.
    '''
    scene_id = int(scene['name'].replace('scene-', ''))   #419 la que data empieza en frame 4 data.frame_id.unique() token '8c84164e752a4ab69d039a07c898f7af'
    data = pd.DataFrame(columns=['scene_id',
                                 'sample_token',
                                 'frame_id',
                                 'type',
                                 'node_id',
                                 'x_global',
                                 'y_global', 
                                 'heading',
                                 'vel_x',
                                 'vel_y',
                                 'acc_x',
                                 'acc_y',
                                 'heading_change_rate',
                                 'length',
                                 'width',
                                 'height'])
    sample_token = scene['first_sample_token']
    sample = nuscenes.get('sample', sample_token)
    frame_id = 0
    mean_xy = []
    while sample['next']:
        if frame_id != 0:
            sample = nuscenes.get('sample', sample['next'])
            sample_token = sample['token']
        annotations = helper.get_annotations_for_sample(sample_token)
        for i,annotation in enumerate(annotations):
            instance_token = annotation['instance_token']
            #ann = instance_token + '_' + sample_token
            #if ann not in scene_annotations:
            if instance_token not in instances:
                continue

            category = annotation['category_name']
            #attribute = nuscenes.get('attribute', annotation['attribute_tokens'][0])['name']

            if 'pedestrian' in category:
                node_type = 2
            elif 'bicycle' in category or 'motorcycle' in category: #and 'without_rider' not in attribute:
                node_type = 3
            elif 'vehicle' in category: #filter parked vehicles                
                node_type = 1
            else:
                node_type = 0

            #if first sample returns nan
            heading_change_rate = helper.get_heading_change_rate_for_agent(instance_token, sample_token)
            velocity =  helper.get_velocity_for_agent(instance_token, sample_token)
            acceleration = helper.get_acceleration_for_agent(instance_token, sample_token)
            

            data_point = pd.Series({'scene_id': scene_id,
                                    'sample_token': sample_token,
                                    'frame_id': frame_id,
                                    'type': node_type,
                                    'node_id': instance_token,
                                    'x_global': annotation['translation'][0],
                                    'y_global': annotation['translation'][1],
                                    'heading': Quaternion(annotation['rotation']).yaw_pitch_roll[0],
                                    'vel_x': velocity[0],
                                    'vel_y': velocity[1],
                                    'acc_x': acceleration[0],
                                    'acc_y': acceleration[1],
                                    'heading_change_rate': heading_change_rate,
                                    'length': annotation['size'][0],
                                    'width': annotation['size'][1],
                                    'height': annotation['size'][2]}).fillna(0)   #inplace=True         

            data = data.append(data_point, ignore_index=True)

        if not data.empty:
            # Ego Vehicle
            sample_data = nuscenes.get('sample_data', sample['data']['CAM_FRONT'])
            annotation = nuscenes.get('ego_pose', sample_data['ego_pose_token'])
            data_point = pd.Series({'scene_id': scene_id,
                                    'sample_token': sample_token,
                                    'frame_id': frame_id,
                                    'type': 0,
                                    'node_id': sample_data['ego_pose_token'],
                                    'x_global': annotation['translation'][0],
                                    'y_global': annotation['translation'][1],
                                    'heading': Quaternion(annotation['rotation']).yaw_pitch_roll[0],
                                    'vel_x': 0,
                                    'vel_y': 0,
                                    'acc_x': 0,
                                    'acc_y': 0,
                                    'heading_change_rate': 0,
                                    'length': 4,
                                    'width': 1.7,
                                    'height': 1.5})
                                   
            data = data.append(data_point, ignore_index=True)

        frame_id += 1

    #data.sort_values('frame_id', inplace=True)
    tracks_per_frame=data.groupby(['frame_id'], sort=True)
    '''
    Tracks is a list of n_frames rows ordered by frame.
    Each row contains a dict, where each key corresponds to an array of data from all agents in that frame.
    '''
    tracks = []
    for frame, track_rows in tracks_per_frame:
        #track_rows contains info of all agents in frame
        track = track_rows.to_dict(orient="list")
        
        for key, value in track.items():
            if key not in ["frame_id", "scene_id", "node_id", "sample_token"]:
                track[key] = np.array(value)
            
        track['info_sequence'] = np.stack([track["frame_id"],track["scene_id"]], axis=-1)
        track['info_agent'] = np.stack([track["type"],track["length"],track["width"],track["height"]], axis=-1)
        track["position"] = np.stack([track["x_global"], track["y_global"], track["heading"]], axis=-1)
        track['motion'] = np.stack([track["vel_x"], track["vel_y"], track["acc_x"],track["acc_y"], track["heading_change_rate"]], axis=-1)
        track["bbox"] = calculate_rotated_bboxes(track["x_global"], track["y_global"],
                                                track["length"], track["width"],
                                                np.deg2rad(track["heading"]))
    
        tracks.append(track)

    if tracks[-1]['frame_id'][0] - tracks[0]['frame_id'][0] != len(tracks)-1:
        print(f"{ tracks[-1]['frame_id'][0] - tracks[0]['frame_id'][0]} != {len(tracks)-1} in scene {scene_id},{scene_token}")
    #assert tracks[-1]['frame_id'][0] - tracks[0]['frame_id'][0] == len(tracks)-1, f"{ tracks[-1]['frame_id'][0] - tracks[0]['frame_id'][0]} != {len(frame_id_list)} in scene {scene_id},{scene_token}"
    
    all_feature_list = []
    all_adjacency_list = []
    all_mean_list = []
    tokens_list = []
    maps_list = []
    visible_object_indexes_list=[]

    #generate tracks with only 0.5 or 1s of history
    #for i in [1,2]:
    #    start_ind=0
    #    current_frame=i

    for i, [frame, track] in enumerate(tracks_per_frame):
        sample_token = track['sample_token'].values[0]
        if sample_token in samples:
            current_ind = i #start_ind + history_frames -1   #0,8,16,24
            start_ind = current_ind - history_frames
            end_ind = current_ind + future_frames if (current_ind + future_frames) <= len(tracks)-1 else len(tracks)-1
            object_frame_feature, neighbor_matrix, mean_xy, inst_sample_tokens = process_tracks(tracks, start_ind, end_ind, current_ind, sample_token, scene_annotations)  
            
            #HD MAPs
            '''
            # Retrieve ego_vehicle pose
            sample_token = tracks[current_ind]['sample_token'][0]
            sample_record = nuscenes.get('sample', sample_token)     
            sample_data_record = nuscenes.get('sample_data', sample_record['data']['LIDAR_TOP'])
            poserecord = nuscenes.get('ego_pose', sample_data_record['ego_pose_token'])
            poserecord['instance_token'] = sample_data_record['ego_pose_token']
            
            maps = np.array( [input_representation.make_input_representation(instance, sample_token, poserecord, ego=False) for instance in tracks[current_ind]["node_id"][:-1]] )   #[N_agents,500,500,3] uint8 range [0,256] 
            maps = np.vstack((maps, np.expand_dims( input_representation.make_input_representation(tracks[current_ind]["node_id"][-1], sample_token, poserecord, ego=True), axis=0) ))
        
            maps = np.array( F.interpolate(torch.tensor(maps.transpose(0,3,1,2)), size=224) ).transpose(0,2,3,1)

            save_path_map = os.path.join(base_path_map, sample_token + '.pkl')
            with open(save_path_map, 'wb') as writer:
                pickle.dump(maps,writer)  
            '''
            all_feature_list.append(object_frame_feature)
            all_adjacency_list.append(neighbor_matrix)	
            all_mean_list.append(mean_xy)
            tokens_list.append(inst_sample_tokens)


    all_adjacency = np.array(all_adjacency_list)
    all_mean = np.array(all_mean_list)                            
    all_feature = np.array(all_feature_list)
    tokens = np.array(tokens_list, dtype=object)
    return all_feature, all_adjacency, all_mean, tokens
    


# Data splits for the CHALLENGE - returns instance and sample token  

# Train: 5883 seq (475 scenes) Train_val: 2219 seq (185 scenes)  Val: 1682 seq (138 scenes) 
ns_scene_names = dict()
ns_scene_names['train'] = get_prediction_challenge_split("train", dataroot=DATAROOT) 
ns_scene_names['val'] =  get_prediction_challenge_split("train_val", dataroot=DATAROOT)
ns_scene_names['test'] = get_prediction_challenge_split("val", dataroot=DATAROOT)  #9041


#scenes_df=[]
#nuscenes.field2token('scene', 'name','scene-0')[0]

for data_class in ['test']:
    scenes_token_set=set()
    samples = []
    instances = []
    annotations = []
    for ann in ns_scene_names[data_class]:
        instance_token, sample_token=ann.split("_")
        sample = nuscenes.get('sample', sample_token)
        scenes_token_set.add(nuscenes.get('scene', sample['scene_token'])['token'])
        instances.append(instance_token)   #Instances in prediction_challenge.json 789
        samples.append(sample_token)  #3076
        annotations.append(ann)
    all_data = []
    all_adjacency = []
    all_mean_xy = []
    all_tokens = []
    
    for scene_token in scenes_token_set:
        all_feature_sc, all_adjacency_sc, all_mean_sc, tokens_sc = process_scene(nuscenes.get('scene', nuscenes.field2token('scene', 'name','scene-0272')[0]), samples, instances, annotations)   # 780 scene_token = '656bb27689dc4e9b8e4559e3f6a7e534'
        #process_scene(nuscenes.get('scene', scene_token), samples, instances)
        print(f"Scene {nuscenes.get('scene', scene_token)['name']} processed!")# {all_adjacency_sc.shape[0]} sequences of 8 seconds.")
    
        all_data.extend(all_feature_sc)
        all_adjacency.extend(all_adjacency_sc)
        all_mean_xy.extend(all_mean_sc)
        all_tokens.extend(tokens_sc)
        #scenes_df.append(scene_df)
        #scene_df.to_csv(os.path.join('./nuscenes_processed/', nuscenes.get('scene', scene_token)['name'] + '.csv'))
    all_data = np.array(all_data)  
    all_adjacency = np.array(all_adjacency) 
    all_mean_xy = np.array(all_mean_xy) 
    all_tokens = np.array(all_tokens)
    save_path = '/media/14TBDISK/sandra/nuscenes_processed/ns_challenge_json_3s_' + data_class + '.pkl'
    with open(save_path, 'wb') as writer:
        pickle.dump([all_data, all_adjacency, all_mean_xy, all_tokens], writer)
    print(f'Processed {all_data.shape[0]} sequences and {len(scenes_token_set)} scenes.')

