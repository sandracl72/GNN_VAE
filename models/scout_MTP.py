import sys
sys.path.append('..')
import dgl
from dgl import function as fn
from dgl.nn.pytorch.conv.gatconv import edge_softmax, Identity, expand_as_pair
import torch
import torch.nn as nn
import torch.nn.functional as F
import os
os.environ['DGLBACKEND'] = 'pytorch'
from torch.utils.data import DataLoader
from NuScenes.nuscenes_Dataset import nuscenes_Dataset, collate_batch_ns
from torchvision.models import resnet18
from torchsummary import summary
from models.MapEncoder import My_MapEncoder, ResNet18, ResNet50
from models.backbone import MobileNetBackbone, ResNetBackbone, calculate_backbone_feature_dim
from utils import MTPLoss
from nuscenes.nuscenes import NuScenes
from nuscenes.prediction import PredictHelper


from captum.attr import DeepLiftShap, DeepLift, FeaturePermutation, LayerDeepLift
import shap


class PositionalEncoding(nn.Module):

    def __init__(self, d_model, dropout=0.1, max_len=7):
        super(PositionalEncoding, self).__init__()
        self.dropout = nn.Dropout(p=dropout)
        '''
        pe = torch.zeros(max_len, d_model)   #T,512
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)  #T,1
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))  #256
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0)#.transpose(0, 1)   #1,T,512
        self.register_buffer('pe', pe)
        '''
        self.pe = nn.Parameter(torch.randn(1,max_len, d_model))

    def forward(self, x):
        x = x + self.pe[:,:x.size(1), :]  #x is N,T,512 + (1,T,512) 
        return self.dropout(x)


class GATConv(nn.Module):
    def __init__(self,
                 in_feats,
                 ew_dims,
                 out_feats,
                 num_heads,
                 feat_drop=0.6,
                 attn_drop=0.6,
                 negative_slope=0.2,
                 att_ew=False,
                 residual=False,
                 activation=F.elu):
        super(GATConv, self).__init__()
        self._num_heads = num_heads
        self._in_src_feats, self._in_dst_feats = expand_as_pair(in_feats)
        self._out_feats = out_feats
        self.fc = nn.Linear(self._in_src_feats, out_feats * num_heads, bias=False)
        if att_ew:
            self.attn_ew = nn.Parameter(torch.FloatTensor(size=(1, num_heads, ew_dims)))
        else:
            self.register_buffer('attn_ew', None)
        self.attn_l = nn.Parameter(torch.FloatTensor(size=(1, num_heads, out_feats)))
        self.attn_r = nn.Parameter(torch.FloatTensor(size=(1, num_heads, out_feats)))
        self.feat_drop = nn.Dropout(feat_drop)
        self.attn_drop = nn.Dropout(attn_drop)
        self.leaky_relu = nn.LeakyReLU(negative_slope)
        if residual:
            if self._in_dst_feats != out_feats:
                self.res_fc = nn.Linear(self._in_dst_feats, num_heads * out_feats, bias=False)
            else:
                self.res_fc = Identity()
        else:
            self.register_buffer('res_fc', None)
        self.reset_parameters()
        self.activation = activation

    def reset_parameters(self):
        """
        Description
        -----------
        Reinitialize learnable parameters.
        Note
        ----
        The fc weights are initialized using Glorot uniform initialization.
        The attention weights are using xavier initialization method.
        """
        gain = nn.init.calculate_gain('relu')
        if hasattr(self, 'fc'):
            nn.init.xavier_normal_(self.fc.weight, gain=gain)
        else:
            nn.init.xavier_normal_(self.fc_src.weight, gain=gain)
            nn.init.xavier_normal_(self.fc_dst.weight, gain=gain)
        nn.init.xavier_normal_(self.attn_l, gain=gain)
        nn.init.xavier_normal_(self.attn_r, gain=gain)
        if self.attn_ew is not None:
            nn.init.xavier_normal_(self.attn_ew, gain=gain)
        if isinstance(self.res_fc, nn.Linear):
            nn.init.xavier_normal_(self.res_fc.weight, gain=gain)

    def forward(self, graph, feat, e_w, get_attention=False):
        with graph.local_scope():
            h_src = h_dst = self.feat_drop(feat)
            feat_src = feat_dst = self.fc(h_src).view(-1, self._num_heads, self._out_feats)

            el = (feat_src * self.attn_l).sum(dim=-1).unsqueeze(-1)  
            er = (feat_dst * self.attn_r).sum(dim=-1).unsqueeze(-1) 
            
            graph.srcdata.update({'ft': feat_src, 'el': el})
            graph.dstdata.update({'er': er})
            # compute edge attention, el and er are a_l Wh_i and a_r Wh_j respectively.
            graph.apply_edges(fn.u_add_v('el', 'er', 'e'))
            if self.attn_ew is not None:
                ew = (e_w.view(-1, self._num_heads, e_w.shape[-1]) * self.attn_ew).sum(dim=-1).unsqueeze(-1)
                graph.edata['e'] = graph.edata['e'] + ew
            e = self.leaky_relu(graph.edata.pop('e'))
            # compute softmax
            graph.edata['a'] = self.attn_drop(edge_softmax(graph, e))
            # message passing
            graph.update_all(fn.u_mul_e('ft', 'a', 'm'),
                             fn.sum('m', 'ft'))
            rst = graph.dstdata['ft']
            # residual
            if self.res_fc is not None:
                resval = self.res_fc(h_dst).view(h_dst.shape[0], -1, self._out_feats)
                rst = rst + resval
            # activation
            if self.activation:
                rst = self.activation(rst)
            if self._num_heads == 1:
                rst = rst.squeeze(1)
            if get_attention:
                return rst, graph.edata['a']
            else:
                return rst


class My_GATLayer(nn.Module):
    def __init__(self, in_feats, out_feats, e_dims, relu=True, feat_drop=0., attn_drop=0., att_ew=False, res_weight=True, res_connection=True):
        super(My_GATLayer, self).__init__()
        self.linear_self = nn.Linear(in_feats, out_feats, bias=False)
        self.linear_func = nn.Linear(in_feats, out_feats, bias=False)
        self.att_ew=att_ew
        self.relu = relu
        if att_ew:
            self.attention_func = nn.Linear(2 * out_feats + e_dims, 1, bias=False)
        else:
            self.attention_func = nn.Linear(2 * out_feats, 1, bias=False)
        self.feat_drop_l = nn.Dropout(feat_drop)
        self.attn_drop_l = nn.Dropout(attn_drop)   
        self.res_con = res_connection
        self.leaky_relu = nn.LeakyReLU(0.2)
        self.reset_parameters()

      
    def reset_parameters(self):
        """Reinitialize learnable parameters."""
        gain = torch.nn.init.calculate_gain('linear', param=None)
        nn.init.xavier_normal_(self.linear_self.weight, gain)
        nn.init.xavier_normal_(self.linear_func.weight, gain)
        ##nn.init.kaiming_normal_(self.linear_self.weight, a=0.2, nonlinearity='leaky_relu')
        ##nn.init.kaiming_normal_(self.linear_func.weight, a=0.2, nonlinearity='leaky_relu')
        nn.init.kaiming_normal_(self.attention_func.weight, a=0.2, nonlinearity='leaky_relu')
        
    
    def edge_attention(self, edges):
        concat_z = torch.cat([edges.src['z'], edges.dst['z']], dim=-1) #(n_edg,hid)||(n_edg,hid) -> (n_edg,2*hid) 
        
        if self.att_ew:
           concat_z = torch.cat([edges.src['z'], edges.dst['z'], edges.data['w']], dim=-1) 
        
        src_e = self.attention_func(concat_z)  #(n_edg, 1) att logit
        return {'e': self.leaky_relu(src_e)}
    
    def message_func(self, edges):
        return {'z': edges.src['z'], 'e':edges.data['e']}
        
    def reduce_func(self, nodes):
        h_s = nodes.data['h_s']      
        #Attention score
        a = self.attn_drop_l(   F.softmax(nodes.mailbox['e'], dim=1)  )  #attention score between nodes i and j
        h = h_s + torch.sum(a * nodes.mailbox['z'], dim=1)
        return {'h': h}
                               
    def forward(self, g, h, ew):
        with g.local_scope():
            h_in = h.clone()
            g.edata['w']  = ew 
            g.ndata['h']  = h 
            #feat dropout
            h=self.feat_drop_l(h)
            g.ndata['h_s'] = self.linear_self(h) 
            g.ndata['z'] = self.linear_func(h) 
            g.apply_edges(self.edge_attention)
            g.update_all(self.message_func, self.reduce_func)
            h = g.ndata['h'] #+g.ndata['h_s'] 
            #h = h * snorm_n # normalize activation w.r.t. graph node size
            if self.relu:
                h = F.elu(h)  
            if self.res_con:
                h = h_in + h 
            return h #graph.ndata.pop('h') - another option to g.local_scope()


class MultiHeadGATLayer(nn.Module):
    def __init__(self, in_feats, out_feats, num_heads, e_dims, relu=True, merge='cat',  feat_drop=0., attn_drop=0., att_ew=False, res_weight=True, res_connection=True):
        super(MultiHeadGATLayer, self).__init__()
        self.heads = nn.ModuleList()
        for i in range(num_heads):
            self.heads.append( GATConv(in_feats, e_dims, out_feats, 1, feat_drop, attn_drop, residual=True, att_ew=att_ew, activation=F.elu) )
            #self.heads.append(My_GATLayer(in_feats, out_feats, e_dims, feat_drop=feat_drop, attn_drop=attn_drop, att_ew=att_ew, res_weight=res_weight, res_connection=res_connection))
        self.merge = merge

    def forward(self, g, h, e_w):
        if isinstance(h, list):
            head_outs = [attn_head(g, h_mode, e_w) for attn_head, h_mode in zip(self.heads, h)]
        else:
            head_outs = [attn_head(g, h, e_w) for attn_head in self.heads]
            
        if self.merge == 'cat':
            # concat on the output feature dimension (dim=1), for intermediate layers
            return torch.cat(head_outs, dim=1)
        elif self.merge == 'list':
            return head_outs
        else:
            # merge using average, for final layer
            return torch.mean(torch.stack(head_outs, dim=1),dim=1)

    
class SCOUT_MTP(nn.Module):
    
    def __init__(self, input_dim, hidden_dim, emb_dim, output_dim, dropout=0.2, bn=False, gn=False, 
                feat_drop=0., attn_drop=0., heads=1,att_ew=False, res_weight=True, emb_type = 'emb',
                res_connection=True, ew_dims=2,  backbone='mobilenet', freeze=0, num_modes=3, history_frames=7):
        super().__init__()

        self.heads = heads
        self.bn = bn
        self.gn = gn
        self.hidden_dim = hidden_dim
        self.emb_dim = emb_dim
        self.emb_type = emb_type
        self.output_dim = output_dim
        self.ew_dims = ew_dims
        self.backbone = backbone
        self.num_modes = num_modes
        
        ###############
        # Map Encoder #
        ###############
        
        if backbone == 'map_encoder':            
            self.feature_extractor = My_MapEncoder(input_channels = 1, input_size=112, 
                                                    hidden_channels = [10,32,64,128,256], output_size = hidden_dim, 
                                                    kernels = [5,5,3,3,3], strides = [1,2,2,2,2])
            hidden_dims = hidden_dim*2
            '''
            model_ft = resnet18(pretrained=False)
            model_ft.conv1 = nn.Conv2d(1, 64, kernel_size=7, stride=1, padding=3,
                               bias=False)
            nn.init.kaiming_normal_(model_ft.conv1.weight, mode='fan_out', nonlinearity='relu')
            self.feature_extractor = torch.nn.Sequential(*list(model_ft.children())[:-1]) 
            hidden_dims = hidden_dim+512
            '''

        elif backbone == 'mobilenet':       
            feature_extractor = MobileNetBackbone('mobilenet_v2', freeze = freeze)  #returns [n,1280] # 18 layers
            #self.hidden_dim = hidden_dim + 1280

        elif backbone == 'resnet18':       
            feature_extractor = ResNetBackbone('resnet18', freeze = freeze) #ResNet18(hidden_dim, freeze)  [n, 512] #9 layers (con avgpool) - if freeze=8 train last conv block
            #self.hidden_dim = hidden_dim + hidden_dim * 2
        elif backbone == 'resnet34':       
            feature_extractor = ResNetBackbone('resnet34', freeze = freeze) 
        elif backbone == 'resnet50':       
            feature_extractor = ResNetBackbone('resnet50', freeze = freeze) #ResNet50(hidden_dim, freeze)  #[n,2048] #9 layers
            #self.hidden_dim = hidden_dim + hidden_dim * 2

        elif backbone == 'resnet_gray':
            resnet = resnet18(pretrained=False)
            modules = list(resnet.children())[:-3]
            modules.append(torch.nn.AdaptiveAvgPool2d((1, 1))) 
            modules[0] = nn.Conv2d(1, 64, kernel_size=7, stride=2, padding=3,bias=False)
            nn.init.kaiming_normal_(modules[0].weight, mode='fan_out', nonlinearity='relu')
            feature_extractor=torch.nn.Sequential(*modules)   
            self.hidden_dim = hidden_dim + 256

        else:
            feature_extractor = None
            emb_dim = hidden_dim

        backbone_feature_dim = calculate_backbone_feature_dim(feature_extractor, input_shape = (3,224,224)) if backbone != 'None' else 0
        '''
        embedding_h = nn.Linear(input_dim, backbone_feature_dim//4)###//2)
        self.hidden_dim = backbone_feature_dim//8 + backbone_feature_dim # hidden_dim + backbone_feature_dim
        encode_h = nn.GRU( backbone_feature_dim//4,  backbone_feature_dim//8, batch_first=True)
        linear_cat = nn.Linear(self.hidden_dim, hidden_dim)
        self.hidden_dim = hidden_dim
        '''
        if emb_type == 'gru':
            embedding_h = nn.Linear(input_dim, emb_dim//2)     #self.embedding_h = nn.Linear(input_dim+64, hidden_dim)        
            encode_h = nn.GRU(emb_dim//2, emb_dim, batch_first=True)
        elif emb_type == 'pos_enc':
            embedding_h = nn.Linear(input_dim, emb_dim)
            encode_h = PositionalEncoding(emb_dim, dropout)
        else:
            embedding_h = nn.Linear(input_dim, emb_dim) #nn.Embedding(11, emb_dim)

        linear_cat = nn.Linear(emb_dim + backbone_feature_dim, hidden_dim)
        self.hidden_dim = hidden_dim
        
        #self.embedding_e = nn.Linear(2, hidden_dims) if  ew_type else nn.Linear(1, hidden_dims)
        resize_e = nn.ReplicationPad1d(self.hidden_dim//2-1)
        resize_e2 = nn.ReplicationPad1d(self.hidden_dim * heads//2-1)

        if bn:
            self.batch_norm = nn.BatchNorm1d(hidden_dim)
        elif gn:
            self.group_norm = nn.GroupNorm(32, hidden_dim) 

        if heads == 1:
            gat_1 = GATConv(hidden_dim, ew_dims, hidden_dim, 1, feat_drop, attn_drop, residual=True, att_ew=att_ew, activation=F.elu) #My_GATLayer(self.hidden_dim, self.hidden_dim, e_dims = self.hidden_dim//2*2, feat_drop=feat_drop, attn_drop=attn_drop, att_ew=att_ew, res_weight=res_weight, res_connection=res_connection) #GATConv(hidden_dim, hidden_dim, 1,feat_drop, attn_drop,residual=True, activation=torch.relu) 
            #gat_2 = My_GATLayer(self.hidden_dim, self.hidden_dim,  e_dims = self.hidden_dim//2*2, feat_drop=feat_drop, attn_drop=attn_drop, att_ew=att_ew, res_weight=res_weight, res_connection=res_connection)  #GATConv(hidden_dim, hidden_dim, 1,feat_drop, attn_drop,residual=True, activation=torch.relu)
            gat_2 = MultiHeadGATLayer(self.hidden_dim, self.hidden_dim,e_dims=ew_dims, res_weight=res_weight, merge='cat', res_connection=res_connection ,num_heads=self.num_modes, feat_drop=0., attn_drop=0., att_ew=att_ew) #GATConv(hidden_dim*heads, hidden_dim*heads, heads,feat_drop, attn_drop,residual=True, activation='relu')
            #linear1 = nn.Linear(self.hidden_dim, output_dim * self.num_modes)
        else:
            gat_1 = MultiHeadGATLayer(self.hidden_dim, self.hidden_dim, e_dims=ew_dims,res_weight=res_weight, merge='cat', res_connection=res_connection , num_heads=heads,feat_drop=feat_drop, attn_drop=attn_drop, att_ew=att_ew) #GATConv(hidden_dim, hidden_dim, heads,feat_drop, attn_drop,residual=True, activation='relu')
            #self.embedding_e2 = nn.Linear(2, hidden_dims*heads) if ew_type else nn.Linear(1, hidden_dims*heads)
            gat_2 = MultiHeadGATLayer(self.hidden_dim*heads, self.hidden_dim*heads,e_dims=ew_dims, res_weight=res_weight, merge='cat', res_connection=res_connection ,num_heads=self.num_modes, feat_drop=0., attn_drop=0., att_ew=att_ew) #GATConv(hidden_dim*heads, hidden_dim*heads, heads,feat_drop, attn_drop,residual=True, activation='relu')
        
        linear1 = nn.Linear(self.hidden_dim * heads * self.num_modes * history_frames, output_dim * self.num_modes) if self.emb_type=='pos_enc' else nn.Linear(self.hidden_dim * heads * self.num_modes, output_dim * self.num_modes) #nn.ModuleList()
            
            
        if dropout:
            self.dropout_l = nn.Dropout(dropout, inplace=False)
        else:
            self.dropout_l = nn.Dropout(0.)
        
        self.leaky_relu = nn.LeakyReLU(0.1) 
        self.embeddings = nn.ModuleDict({
            'embedding_h': embedding_h,
            'map_encoder': feature_extractor,
            'linear_cat': linear_cat
        })
        if self.emb_type != 'emb':
            self.embeddings['encode_h'] = encode_h
        
        self.base = nn.ModuleDict({
          'resize_e': resize_e,
          'gat1': gat_1,
          'resize_e2': resize_e2,
          'gat2': gat_2
        })
        '''
        ###############
        # DECODER_GRU #
        ###############
        # OPTION 1  
        dec_gru = nn.GRUCell(self.hidden_dim * heads * self.num_modes, emb_dim * num_modes)
        # Once we decode over T frames we output final traj + prob
        linear1 = nn.Linear(5 * emb_dim * self.num_modes, self.output_dim * self.num_modes ) 
        # OPTION 2  ,  gat_2 merge=list
        ##dec_gru = nn.GRUCell(self.hidden_dim * heads, emb_dim)
        # Once we decode over T frames we output final traj + prob
        ##linear1 = nn.Linear(5 * emb_dim * self.num_modes, self.output_dim * self.num_modes)  #A) nn.Linear(5 * emb_dim * self.num_modes, self.output_dim) 
        self.base['dec'] = dec_gru
        '''
        self.base['linear1'] = linear1
        
        self.reset_parameters()

    def reset_parameters(self):
        """Reinitialize learnable parameters."""
        if self.bn:
            nn.init.constant_(self.batch_norm.weight, 1)
            nn.init.constant_(self.batch_norm.bias, 0)
        elif self.gn:
            nn.init.constant_(self.group_norm.weight, 1)
            nn.init.constant_(self.group_norm.bias, 0)
        #nn.init.kaiming_normal_(self.embedding_h[0].weight, nonlinearity='leaky_relu', a=0.2)
        nn.init.kaiming_normal_(self.embeddings['embedding_h'].weight, torch.nn.init.calculate_gain('linear'))
        ''' 
        OTRA OPCION
        fan_in = self.embeddings['embedding_h'].in_features
        nn.init.normal(self.embeddings['embedding_h'].weight, 0, sqrt(1. / fan_in))
        '''
        nn.init.kaiming_normal_(self.base['linear1'].weight, nonlinearity='relu')
        nn.init.xavier_normal_(self.embeddings['linear_cat'].weight)       
        #if self.heads > 1:
        #    nn.init.xavier_normal_(self.embedding_e2.weight)
    
    def inference(self, g, feats, e_w,snorm_n,snorm_e, maps):
        y=self.forward(g, feats, e_w, snorm_n, snorm_e, maps)
        return y

    def forward(self, feats,e_w, maps, g, attr=False):
        # For attributions (explain.py)
        if attr:
            g2 = g
            g = dgl.batch((g, g2))

        # Input embedding
        if self.emb_type =='gru':
            h_enc = self.embeddings['encode_h'](F.selu(self.embeddings['embedding_h'](feats)))[1].squeeze(dim=0)
        elif self.emb_type == 'pos_enc':
            h_enc = self.embeddings['encode_h'](F.selu(self.embeddings['embedding_h'](feats)))  # N 7 512
        else:
            #reshape to have shape (B*V,T*C) [c1,c2,...,c6]
            h = self.embeddings['embedding_h'](feats)
        if self.backbone != 'None':
            # Maps feature extraction
            maps_embedding = self.embeddings['map_encoder'](maps)  
            if self.emb_type == 'pos_enc':
                maps_embedding = maps_embedding.unsqueeze(1).repeat(1,h.shape[1],1)
            # Embeddings concatenation
            h = torch.cat([maps_embedding, h], dim=-1)
            h = self.embeddings['linear_cat'](h)
        
        #h = F.relu(h)
        if self.bn:
            h = self.batch_norm(h)
            h = F.relu(h)
        elif self.gn:
            h = self.group_norm(h)

        # GAT Layers
        '''
        if self.ew_dims:
            e = self.base['resize_e'](torch.unsqueeze(e_w,dim=1)).flatten(start_dim=1) #self.embedding_e(e_w)
        else:
            e = torch.ones((1, self.hidden_dim), device=h.device) * e_w
        g.edata['w'] = e 
        '''
        h = self.base['gat1'](g, h, e_w) 
        '''
        if self.heads > 1:
            if self.ew_dims:
                e = self.base['resize_e2'](torch.unsqueeze(e_w,dim=1)).flatten(start_dim=1) #self.embedding_e2(e_w)
            else:
                e = torch.ones((1, self.hidden_dim*self.heads), device=h.device) * e_w
            g.edata['w'] = e 
        '''
        h_modes = self.base['gat2'](g, h, e_w)  #BN Y RELU DENTRO DE LA GAT_LAYER
        '''
        y = torch.zeros(5, feats.shape[0], self.emb_dim * self.num_modes).float().to(h_modes.device)
        h = h_enc.repeat(1,self.num_modes)
        #y = torch.zeros((self.num_modes, 5, feats.shape[0], self.emb_dim), device=h.device)
        #y_out = torch.zeros((self.num_modes, feats.shape[0], self.output_dim), device=h.device)
        #for i, mode in enumerate(h_modes):
        for t in range(5):
            h = self.base['dec'](h_modes, h)
            y[t] = h
        '''
        y = self.dropout_l(h_modes)
        
        #y =  self.base['linear1'](h_modes)
        if self.emb_type == 'pos_enc':
            y = y.view(y.shape[0],-1)
        
        y = self.base['linear1'](y)
        
        mode_probabilities = torch.cat([y[:, self.output_dim * i - 1].unsqueeze(1) for i in range(1,self.num_modes+1)], dim=1)
        predictions = torch.cat([y[:, self.output_dim * (i-1) : self.output_dim*i - 1]  for i in  range(1,self.num_modes+1)], dim=1)
        ##mode_probabilities = y_out[:, :, - 1].transpose(0,1)
        ##predictions = y_out[:,:,:-1].contiguous().view(feats.shape[0],-1)

        # Normalize the probabilities to sum to 1 for inference.
        ##mode_probabilities = y[:, -self.num_modes:].clone()
        ##predictions = y[:, :-self.num_modes]

        if not self.training:
            mode_probabilities = F.softmax(mode_probabilities, dim=-1)

        return torch.cat((predictions, mode_probabilities), 1)
        

if __name__ == '__main__':

    history_frames = 9
    future_frames = 12
    hidden_dims = 256
    heads = 2
    emb_type = 'emb'

    input_dim = 6 if emb_type != 'emb' else 6*(history_frames) + 3
    output_dim = 2*future_frames + 1

    model = SCOUT_MTP(input_dim=input_dim, hidden_dim=hidden_dims, emb_dim=512, emb_type=emb_type, output_dim=output_dim, heads=heads,  ew_dims= 2,
                   dropout=0.1, bn=False, feat_drop=0., attn_drop=0., att_ew=True, backbone='mobilenet', freeze=True)
    
    #DATAROOT = '/media/14TBDISK/nuscenes'
    #nuscenes = NuScenes('v1.0-trainval', dataroot=DATAROOT)   
    #helper = PredictHelper(nuscenes)
    g = dgl.graph(([0, 0, 0, 1, 2, 1, 1], [0, 1, 2, 0, 1, 2, 0]))
    e_w = torch.rand(7, 2)
    snorm_n = torch.rand(3, 1)
    snorm_e = torch.rand(3, 1)
    feats = torch.rand(3, history_frames, 7)
    maps = torch.rand(3, 3, 112, 112)
    #out = model(feats, e_w,  maps, g)

    #desired_shape = (out.shape[0], 3, -1, 2)
    #trajectories_no_modes = out[:, :-3].clone().reshape(desired_shape)

    #off_road = utils.OffRoadRate(helper)
    mtp_loss = MTPLoss(num_modes = 3, regression_loss_weight = 1, angle_threshold_degrees = 5.)
    #summary(model.feature_extractor, input_size=(1,112,112), device='cpu')
    test_dataset = nuscenes_Dataset(train_val_test='test', rel_types=True, history_frames=history_frames, retrieve_lanes=False, local_frame=True) 
    test_dataloader = DataLoader(test_dataset, batch_size=1, shuffle=False,  collate_fn=collate_batch_ns)


    for batch in test_dataloader:
        batched_graph, output_masks,snorm_n, snorm_e, feats, labels_pos, maps,  scene, tokens, mean_xy, global_feats, lanes, static_feats = batch

        ew = batched_graph.edata['w']#.unsqueeze(1)
        feats_model = torch.cat((feats.contiguous().view(feats.shape[0],-1), static_feats),dim = -1)
        pred = model(feats_model, ew, maps, batched_graph)
        predictions = mtp_loss(pred, feats[:,-1,5], labels_pos[:,:,:2].unsqueeze(1), global_feats[:,history_frames-1,:2].unsqueeze(1).unsqueeze(1), output_masks.unsqueeze(1), 
                                            False, tokens, lanes, global_feats[:,history_frames-1], test=True)
                
        '''
        desired_shape = (out.shape[0], 3, -1, 2)
        trajectories_no_modes = out[:, :-3].clone().reshape(desired_shape).transpose(0,1)
        for i, mode in enumerate(trajectories_no_modes):
            trajectories_no_modes[i] =  convert_local_coords_to_global(mode, global_feats[history_frames-1,:2], global_feats[history_frames-1,2]) + mean_xy
        off_road_output = off_road(trajectories_no_modes, str(tokens[0][1]))
        '''
        print(predictions.shape)