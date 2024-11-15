from .pic import VarianceMaskingPIC
import torch.nn as nn
from layers import LatentRateReduction
import torch
from .utils import ste_round
import time

class VarianceMaskingPICREM(VarianceMaskingPIC):

    def __init__(self, 
                N=192, 
                M=640, 
                division_dimension = [320,416],
                dim_chunk = 32,
                multiple_decoder = True,
                multiple_encoder = True,
                multiple_hyperprior = True,
                support_progressive_slices = 5,
                delta_encode = True,
                total_mu_rep = True,
                all_scalable = True,
                mask_policy = "point-based-std",
                check_levels = [0.01,0.25,1.75],
                mu_std = False,
                dimension = "big",
                **kwargs):
        super().__init__(N = N, 
                         M = M , 
                        division_dimension=division_dimension, 
                        dim_chunk=dim_chunk,
                        multiple_decoder=multiple_decoder,
                        multiple_encoder=multiple_encoder, 
                        multiple_hyperprior=multiple_hyperprior,
                        support_progressive_slices=support_progressive_slices,
                        delta_encode=delta_encode,
                        total_mu_rep=total_mu_rep,
                        all_scalable=all_scalable,
                        mask_policy=mask_policy,
                        **kwargs)
        
        self.dimension = dimension
        self.check_levels = check_levels 

        self.enable_rem = True # we start with enabling rems


        self.check_multiple = len(self.check_levels)
        self.mu_std = mu_std


        self.post_latent = nn.ModuleList(
                                nn.ModuleList( LatentRateReduction(dim_chunk = self.base_net.dim_chunk,
                                            mu_std = self.mu_std, dimension=dimension) 
                                            for _ in range(10))
                                for _ in range(self.check_multiple)
                                )



    def extract_chekpoint_representation_from_images(self,x, quality,  rc = True): #fff


        out_latent = self.compress( x, 
                                   quality =self.check_levels[0],
                                    mask_pol ="point-based-std",
                                    real_compress=rc) #["y_hat"] #ddd
            
        if quality == self.check_levels[0]:
            return out_latent["y_hat"]
            
        out_latent_1 = self.compress( x, 
                                quality =self.check_levels[1],
                                mask_pol ="point-based-std",
                                checkpoint_rep= out_latent["y_hat"],
                                real_compress=rc)
            
        if quality == self.check_levels[1]:
            return out_latent_1["y_hat"]
            


        out_latent_2 = self.compress( x, 
                                quality =self.check_levels[2],
                                mask_pol ="point-based-std",
                                checkpoint_rep= out_latent_1["y_hat"],
                                real_compress=rc)
            
        return out_latent_2["y_hat"]




    def find_check_quality(self,quality):
        if quality <= self.check_levels[0]:
            quality_ref = 0 
            quality_post = 0

        elif (len(self.check_levels) == 2 or len(self.check_levels) == 3)  and self.check_levels[0] < quality <= self.check_levels[1]:
                quality_ref = self.check_levels[0]
                quality_post = self.check_levels[1]
        elif len(self.check_levels) == 2 and quality > self.check_levels[1]:
            quality_ref = self.check_levels[1]
            quality_post = 10
        
        elif len(self.check_levels)==3 and  self.check_levels[1] < quality <= self.check_levels[2]:
            quality_ref = self.check_levels[1] 
            quality_post = self.check_levels[-1]
        else:
            quality_ref = self.check_levels[-1]
            quality_post  = 10
        return quality_ref, quality_post



    def apply_latent_enhancement(self,
                                current_index,
                                block_mask,
                                bar_mask,
                                quality,
                                y_b_hat, 
                                mu_scale_base, 
                                mu_scale_enh,
                                mu, 
                                scale,
                                ):



        #bar_mask =   self.masking(scale,pr = quality_bar,mask_pol = mask_pol) 
        #star_mask = self.masking(scale,pr = quality,mask_pol = mask_pol)  

        attention_mask = block_mask - bar_mask 
        attention_mask = self.masking.apply_noise(attention_mask,  training = False)   

        if self.mu_std:
            attention_mask = torch.cat([attention_mask,attention_mask],dim = 1)  
        # in any case I do not improve anithing here!
        if quality <= self.check_levels[0]: #  in case nothing has to be done
            return mu, scale         

        if self.check_multiple == 1:
            enhanced_params =  self.post_latent[0][current_index](y_b_hat, mu_scale_base, mu_scale_enh, attention_mask)
        elif self.check_multiple == 2:
            index = 0 if self.check_levels[0] < quality <= self.check_levels[1] else 1 
            enhanced_params =  self.post_latent[index][current_index](y_b_hat, mu_scale_base, mu_scale_enh, attention_mask)
        else: 
            index = -1 
            if self.check_levels[0] < quality <= self.check_levels[1]: #ffff
                index = 0 
            elif  self.check_levels[1] < quality <= self.check_levels[2]:
                index = 1
            else:
                index = 2 
            enhanced_params =  self.post_latent[index][current_index](y_b_hat, mu_scale_base, mu_scale_enh, attention_mask)   
        if self.mu_std:
                mu,scale = enhanced_params.chunk(2,1)
                return mu, scale
        else:
            scale = enhanced_params
            return mu, scale


    def forward(self, x, mask_pol = "point-based-std", quality = 0, training  = True, checkpoint_ref = None ):


        mask_pol = self.mask_policy if mask_pol is None else mask_pol

        if self.multiple_encoder is False:
            y = self.base_net.g_a(x)
            y_base = y 
            y_enh = y
        else:
            y_base = self.base_net.g_a[0](x)
            y_enh = self.base_net.g_a[1](x)
            y = torch.cat([y_base,y_enh],dim = 1).to(x.device) #dddd

        y_shape = y.shape[2:]
        latent_means, latent_scales, z_likelihoods = self.compute_hyperprior(y, quality)

        y_slices = y.chunk(self.num_slices, 1) # total amount of slicesy,

        y_hat_slices = []
        y_likelihood = []

        mu_base, mu_prog = [],[]
        std_base, std_prog = [],[]

        for slice_index in range(self.num_slice_cumulative_list[0]):
            y_slice = y_slices[slice_index]
            idx = slice_index%self.num_slice_cumulative_list[0]
            indice = min(self.max_support_slices,idx)
            support_slices = (y_hat_slices if self.max_support_slices < 0 else y_hat_slices[:indice]) 
            
            mean_support = torch.cat([latent_means[:,:self.division_dimension[0]]] + support_slices, dim=1)
            scale_support = torch.cat([latent_scales[:,:self.division_dimension[0]]] + support_slices, dim=1) 

            
            mu = self.cc_mean_transforms[idx](mean_support)  #self.extract_mu(idx,slice_index,mean_support)
            mu = mu[:, :, :y_shape[0], :y_shape[1]]  
            scale = self.cc_scale_transforms[idx](scale_support)#self.extract_scale(idx,slice_index,scale_support)
            scale = scale[:, :, :y_shape[0], :y_shape[1]]

            mu_base.append(mu)
            std_base.append(scale) 

            mu_prog.append(mu) #le sommo
            std_prog.append(scale) #le sommo 

            _, y_slice_likelihood = self.gaussian_conditional(y_slice, scale, mu, training = training)
            y_hat_slice = ste_round(y_slice - mu) + mu

            lrp_support = torch.cat([mean_support,y_hat_slice], dim=1)
            lrp = self.lrp_transforms[idx](lrp_support)
            lrp = 0.5 * torch.tanh(lrp)
            y_hat_slice += lrp               

            y_hat_slices.append(y_hat_slice)
            y_likelihood.append(y_slice_likelihood)

        if quality == 0: #and  slice_index == self.num_slice_cumulative_list[0] - 1:
            y_hat = torch.cat(y_hat_slices,dim = 1)
            #x_hat = self.g_s[0](y_hat).clamp_(0, 1) if self.multiple_decoder else self.g_s(y_hat).clamp_(0, 1)
            y_likelihoods = torch.cat(y_likelihood, dim=1)
            return {
                "likelihoods": {"y": y_likelihoods,"z": z_likelihoods},
            "y_hat":y_hat,"y_base":y_hat,"y_complete":y_hat,
            "mu_base":mu_base,"mu_prog":mu_prog,"std_base":std_base,"std_prog":std_prog

            }         

        y_hat_b = torch.cat(y_hat_slices,dim = 1)
        y_hat_slices_quality = []
        

        y_likelihood_quality = []
        y_likelihood_quality = y_likelihood + []#ffff

        y_checkpoint_hat = checkpoint_ref.chunk(10,1) if checkpoint_ref is not None else y_hat_slices


        mu_total = []
        std_total = []

        for slice_index in range(self.ns0,self.ns1):

            y_slice = y_slices[slice_index]
            current_index = slice_index%self.ns0


            if self.delta_encode:
                y_slice = y_slice - y_slices[current_index] 


            support_vector = mu_total if self.all_scalable else y_hat_slices_quality
            support_vector_std = std_total if self.all_scalable else y_hat_slices_quality
            support_slices_mean = self.determine_support(y_hat_slices,
                                                         current_index,
                                                        support_vector                                                      
                                                         )
            support_slices_std = self.determine_support(y_hat_slices,
                                                         current_index,
                                                        support_vector_std                                                      
                                                         )

            mean_support = torch.cat([latent_means[:,self.base_net.dimensions_M[0]:]] + support_slices_mean, dim=1)
            scale_support = torch.cat([latent_scales[:,self.base_net.dimensions_M[0]:]] + support_slices_std, dim=1) 

            mu = self.base_net.cc_mean_transforms_prog[current_index](mean_support)  #self.extract_mu(idx,slice_index,mean_support)
            mut = mu + y_hat_slices[current_index] if self.base_net.total_mu_rep else mu
            mu = mu[:, :, :y_shape[0], :y_shape[1]]  

            scale = self.base_net.cc_scale_transforms_prog[current_index](scale_support)#self.extract_scale(idx,slice_index,scale_support)
            

            mu_prog[current_index] = mu_prog[current_index] + mu
            std_prog[current_index] = std_prog[current_index] +  scale 

            std_total.append(scale)
            mu_total.append(mut)

            scale = scale[:, :, :y_shape[0], :y_shape[1]] #fff


            # qua avviene la magia! 
            ms_base = torch.cat([mu_base[current_index],std_base[current_index]],dim = 1) 
            ms_progressive =  torch.cat([mu,scale],dim = 1) if self.mu_std else scale

            y_b_hat = y_checkpoint_hat[current_index]
            y_b_hat.requires_grad = True

            quality_bar, _  = self.find_check_quality(quality)

            block_mask =  self.masking(scale,pr = quality,mask_pol = mask_pol) # this is the q* in the original paper 
            block_mask = self.masking.apply_noise(block_mask, training)

            bar_mask =   self.masking(scale,pr = quality_bar,mask_pol = mask_pol)
            bar_mask = self.masking.apply_noise(bar_mask, training)

            
            if self.enable_rem:
                mu, scale = self.apply_latent_enhancement(current_index,
                                                        block_mask,
                                                        bar_mask,
                                                        quality,
                                                        y_b_hat, 
                                                        ms_base, 
                                                        ms_progressive,
                                                        mu, 
                                                        scale,
                                                        )
            

            y_slice_m = (y_slice  - mu)*block_mask
            _, y_slice_likelihood = self.base_net.gaussian_conditional(y_slice_m, scale*block_mask, training = training)
            y_hat_slice = ste_round(y_slice - mu)*block_mask + mu


            y_likelihood_quality.append(y_slice_likelihood)


            lrp_support = torch.cat([mean_support,y_hat_slice], dim=1)
            lrp = self.lrp_transforms_prog[current_index](lrp_support)
            lrp = 0.5 * torch.tanh(lrp)
            y_hat_slice += lrp   

            y_hat_slice = self.merge(y_hat_slice,y_hat_slices[current_index])   #ddd
            y_hat_slices_quality.append(y_hat_slice) 

        y_likelihoods = torch.cat(y_likelihood_quality,dim = 1) #ddddd
        y_hat_p = torch.cat(y_hat_slices_quality,dim = 1) 

        mu_base = torch.cat(mu_base,dim = 1)
        mu_prog = torch.cat(mu_prog,dim = 1)
        std_base = torch.cat(std_base,dim = 1)   
        std_prog = torch.cat(std_prog, dim = 1)#kkkk

        index = 0 if quality == 0 else 1
        x_hat = self.base_net.g_s[index](y_hat).clamp_(0, 1) if self.base_net.multiple_decoder  \
                else self.base_net.g_s(y_hat).clamp_(0, 1)

        return {
            "x_hat":x_hat,
            "likelihoods": {"y": y_likelihoods,"z": z_likelihoods},
            "y_hat":y_hat_p,"y_base":y_hat_b,
            "mu_base":mu_base,"mu_prog":mu_prog,"std_base":std_base,"std_prog":std_prog
        }     


    def compress(self, x, 
                quality = 0.0, 
                mask_pol = "point-based-std", 
                checkpoint_rep = None, 
                real_compress = True, 
                used_qual = None):

        used_qual = self.check_levels if used_qual is None else used_qual


        if self.multiple_encoder is False:
            y = self.g_a(x)
        else:
            y_base = self.g_a[0](x)
            y_enh = self.g_a[1](x)
            y = torch.cat([y_base,y_enh],dim = 1).to(x.device)
        y_shape = y.shape[2:]

        z = self.h_a(y)
        z_strings =  self.entropy_bottleneck.compress(z)
        
        z_hat = self.entropy_bottleneck.decompress(z_strings, z.size()[-2:])
        latent_scales = self.h_scale_s(z_hat) if self.multiple_hyperprior is False \
                        else self.h_scale_s[0](z_hat)
        latent_means = self.h_mean_s(z_hat) if self.multiple_hyperprior is False \
                        else self.h_mean_s[0](z_hat)


        if self.multiple_hyperprior and quality > 0:
            latent_scales_enh = self.h_scale_s[1](z_hat) 
            latent_means_enh = self.h_mean_s[1](z_hat)
            latent_means = torch.cat([latent_means,latent_means_enh],dim = 1)
            latent_scales = torch.cat([latent_scales,latent_scales_enh],dim = 1) 

        y_hat_slices = []

        y_slices = y.chunk(self.num_slices, 1) # total amount of slices
        y_strings = []
        masks = []
        mu_base = []
        std_base = [] 


        for slice_index in range(self.ns0):
            y_slice = y_slices[slice_index]
            indice = min(self.max_support_slices,slice_index%self.ns0)


            support_slices = (y_hat_slices if self.max_support_slices < 0 \
                                                        else y_hat_slices[:indice])               
            
            idx = slice_index%self.ns0


            mean_support = torch.cat([latent_means[:,:self.division_dimension[0]]] + support_slices, dim=1)
            scale_support = torch.cat([latent_scales[:,:self.division_dimension[0]]] + support_slices, dim=1) 

            
            mu = self.cc_mean_transforms[idx](mean_support)  #self.extract_mu(idx,slice_index,mean_support)
            mu = mu[:, :, :y_shape[0], :y_shape[1]]  
            scale = self.cc_scale_transforms[idx](scale_support)#self.extract_scale(idx,slice_index,scale_support)
            scale = scale[:, :, :y_shape[0], :y_shape[1]]


            mu_base.append(mu) 
            std_base.append(scale)

            index = self.gaussian_conditional.build_indexes(scale)
            if real_compress:
                y_q_string  = self.gaussian_conditional.compress(y_slice, index,mu)
                y_hat_slice = self.gaussian_conditional.decompress(y_q_string, index)
                y_hat_slice = y_hat_slice + mu
            else:
                y_q_string  = self.gaussian_conditional.quantize(y_slice, "symbols", mu)#ddd
                y_hat_slice = y_q_string + mu

            y_strings.append(y_q_string)

            lrp_support = torch.cat([mean_support,y_hat_slice], dim=1)
            lrp = self.lrp_transforms[idx](lrp_support)
            lrp = 0.5 * torch.tanh(lrp)
            y_hat_slice += lrp

            y_hat_slices.append(y_hat_slice)

        if quality <= 0:
            return {"strings": [y_strings, z_strings],
                    "shape":z.size()[-2:], 
                    "masks":masks,
                    "y":y,
                    "y_hat":torch.cat(y_hat_slices,dim = 1),
                    "latent_means":latent_means,
                    "latent_scales":latent_scales,
                    "mean_base":torch.cat(mu_base,dim = 1),
                    "std_base":torch.cat(std_base,dim = 1)}
        

        y_hat_slices_quality = []

        y_b_hats = checkpoint_rep.chunk(10,1) if checkpoint_rep is not None else y_hat_slices 

        mu_total, std_total = [],[]
        for slice_index in range(self.ns0,self.ns1): #ffff

            y_slice = y_slices[slice_index]
            current_index = slice_index%self.ns0

            if self.delta_encode:
                y_slice = y_slice - y_slices[current_index] 


            support_vector = mu_total if self.all_scalable else y_hat_slices_quality
            support_vector_std = std_total if self.all_scalable else y_hat_slices_quality
            support_slices_mean = self.determine_support(y_hat_slices,
                                                         current_index,
                                                        support_vector                                                      
                                                         )
            support_slices_std = self.determine_support(y_hat_slices,
                                                         current_index,
                                                        support_vector_std                                                      
                                                         )


            
            mean_support = torch.cat([latent_means[:,self.dimensions_M[0]:]] + support_slices_mean, dim=1)
            scale_support = torch.cat([latent_scales[:,self.dimensions_M[0]:]] + support_slices_std, dim=1) 

            mu = self.cc_mean_transforms_prog[current_index](mean_support)  #self.extract_mu(idx,slice_index,mean_support)
            mut = mu + y_hat_slices[current_index] if self.total_mu_rep else mu
            mu = mu[:, :, :y_shape[0], :y_shape[1]]  

            scale = self.cc_scale_transforms_prog[current_index](scale_support)#self.extract_scale(idx,slice_index,scale_support)
            scale = scale[:, :, :y_shape[0], :y_shape[1]] #fff

            std_total.append(scale)
            mu_total.append(mut)

            y_b_hat = y_b_hats[current_index]

            ms_base = torch.cat([mu_base[current_index],std_base[current_index]],dim = 1) 
            ms_progressive =  torch.cat([mu,scale],dim = 1) if self.mu_std else scale
            
            
            
            quality_bar,quality_post = self.find_check_quality(quality)


            block_mask = self.masking(scale,pr = quality,mask_pol = mask_pol)
            block_mask = self.masking.apply_noise(block_mask, False)
            masks.append(block_mask)

            bar_mask = self.masking(scale,pr = quality_bar,mask_pol = mask_pol)
            bar_mask = self.masking.apply_noise(bar_mask, False)


            if self.enable_rem:
                mu, scale = self.apply_latent_enhancement(current_index,
                                                        block_mask,
                                                        bar_mask,
                                                        quality,
                                                        y_b_hat, 
                                                        ms_base, 
                                                        ms_progressive,
                                                        mu, 
                                                        scale,
                                                        )
                
            index = self.gaussian_conditional.build_indexes(scale*block_mask).int()
            if real_compress:

                y_q_string  = self.base_net.gaussian_conditional.compress((y_slice - mu)*block_mask, index)
                y_strings.append(y_q_string)
                y_hat_slice_nomu = self.base_net.gaussian_conditional.quantize((y_slice - mu)*block_mask, "dequantize") 
                y_hat_slice = y_hat_slice_nomu + mu
            else:
                y_q_string  = self.base_net.gaussian_conditional.quantize((y_slice - mu)*block_mask, "dequantize") 
                y_strings.append(y_q_string)
                y_hat_slice = y_q_string + mu



            lrp_support = torch.cat([mean_support,y_hat_slice], dim=1)
            lrp = self.base_net.lrp_transforms_prog[current_index](lrp_support) #ddd
            lrp = 0.5 * torch.tanh(lrp)
            y_hat_slice += lrp

            y_hat_slice = self.merge(y_hat_slice,y_hat_slices[current_index])
            y_hat_slices_quality.append(y_hat_slice)
        
        return {"strings": [y_strings, z_strings],"shape":z.size()[-2:],"masks":masks,"y_hat":torch.cat(y_hat_slices_quality,dim = 1)}
    

    def decompress(self, 
                   strings,
                    shape, 
                    quality,
                    mask_pol = None,
                    checkpoint_rep = None, 
                    ):
        


        mask_pol = self.base_net.mask_policy if mask_pol is None else mask_pol


        start_t = time.time()


        z_hat = self.entropy_bottleneck.decompress(strings[1], shape)
        latent_scales = self.h_scale_s(z_hat) if self.multiple_hyperprior is False    \
                        else self.h_scale_s[0](z_hat)
        latent_means = self.h_mean_s(z_hat) if self.multiple_hyperprior is False \
                        else self.h_mean_s[0](z_hat)

    
        if self.multiple_hyperprior and quality > 0:
            latent_scales_enh = self.h_scale_s[1](z_hat) 
            latent_means_enh = self.h_mean_s[1](z_hat)
            latent_means = torch.cat([latent_means,latent_means_enh],dim = 1)
            latent_scales = torch.cat([latent_scales,latent_scales_enh],dim = 1) 


        y_shape = [z_hat.shape[2] * 4, z_hat.shape[3] * 4]
        y_string = strings[0]
        y_hat_slices = []


        mu_base = []
        std_base = []



        for slice_index in range(self.num_slice_cumulative_list[0]): #ddd
            pr_strings = y_string[slice_index]
            idx = slice_index%self.num_slice_cumulative_list[0]
            indice = min(self.max_support_slices,idx)
            support_slices = (y_hat_slices if self.max_support_slices < 0 else y_hat_slices[:indice]) 
            
            mean_support = torch.cat([latent_means[:,:self.division_dimension[0]]] + support_slices, dim=1)
            scale_support = torch.cat([latent_scales[:,:self.division_dimension[0]]] + support_slices, dim=1) 
      
            mu = self.cc_mean_transforms[idx](mean_support)  #self.extract_mu(idx,slice_index,mean_support)
            mu = mu[:, :, :y_shape[0], :y_shape[1]]  
            scale = self.cc_scale_transforms[idx](scale_support)#self.extract_scale(idx,slice_index,scale_support)
            scale = scale[:, :, :y_shape[0], :y_shape[1]]

            mu_base.append(mu)
            std_base.append(scale)

            index = self.gaussian_conditional.build_indexes(scale)


            rv = self.gaussian_conditional.decompress(pr_strings, index )
            rv = rv.reshape(mu.shape)
            y_hat_slice = self.gaussian_conditional.dequantize(rv, mu)

            

            lrp_support = torch.cat([mean_support, y_hat_slice], dim=1)
            lrp = self.lrp_transforms[idx](lrp_support)
            lrp = 0.5 * torch.tanh(lrp)
            y_hat_slice += lrp

            y_hat_slices.append(y_hat_slice)

        if quality == 0:
            y_hat_b = torch.cat(y_hat_slices, dim=1)

            

            end_t = time.time()
            time_ = end_t - start_t
 
            x_hat = self.g_s[0](y_hat_b).clamp_(0, 1) if self.multiple_decoder else \
                    self.g_s(y_hat_b).clamp_(0, 1)
            return {"x_hat": x_hat, "y_hat": y_hat_slices,"time":time_}


        start_t = time.time()

        y_hat_slices_quality = []


        y_b_hats = checkpoint_rep.chunk(10,1) if checkpoint_rep is not None else y_hat_slices  
        mu_total,std_total = [],[]

        
        for slice_index in range(self.ns0,self.ns1):
            pr_strings = y_string[slice_index]
            current_index = slice_index%self.ns0

            support_slices_mean = self.determine_support(y_hat_slices,
                                                         current_index,
                                                        mu_total if self.all_scalable else y_hat_slices_quality                                                      
                                                         )
            support_slices_std = self.determine_support(y_hat_slices,
                                                         current_index,
                                                        std_total if self.all_scalable else y_hat_slices_quality                                                     
                                                         )


            
            mean_support = torch.cat([latent_means[:,self.dimensions_M[0]:]] + support_slices_mean, dim=1)
            scale_support = torch.cat([latent_scales[:,self.dimensions_M[0]:]] + support_slices_std, dim=1) 

            mu = self.cc_mean_transforms_prog[current_index](mean_support)  #self.extract_mu(idx,slice_index,mean_support)
            mut = mu + y_hat_slices[current_index] if self.total_mu_rep else mu
            mu = mu[:, :, :y_shape[0], :y_shape[1]]  

            scale = self.cc_scale_transforms_prog[current_index](scale_support)#self.extract_scale(idx,slice_index,scale_support)
            
            std_total.append(scale)
            mu_total.append(mut)

            scale = scale[:, :, :y_shape[0], :y_shape[1]] #fff

            y_b_hat =  y_b_hats[current_index]

            #y_b_hat = y_b_hats[current_index]
            ms_base = torch.cat([mu_base[current_index],std_base[current_index]],dim = 1) 
            ms_progressive =  torch.cat([mu,scale],dim = 1) if self.mu_std else scale


            
                   
            quality_bar,quality_post = self.find_check_quality(quality)

            block_mask = self.masking(scale,pr = quality,mask_pol = mask_pol) 
            bar_mask = self.masking(scale,pr = quality_bar,mask_pol = mask_pol)
            
            post_mask = self.masking(scale,pr = quality_post,mask_pol = mask_pol) 



            if self.enable_rem:
                mu, scale = self.apply_latent_enhancement(current_index,
                                                        post_mask,
                                                        bar_mask,
                                                        quality,
                                                        y_b_hat, 
                                                        ms_base, 
                                                        ms_progressive,
                                                        mu, 
                                                        scale,
                                                        )


            
 


            index = self.base_net.gaussian_conditional.build_indexes(scale*block_mask)
            rv = self.base_net.gaussian_conditional.decompress(pr_strings, index)
            rv = rv.reshape(mu.shape)

            y_hat_slice = rv + mu

            lrp_support = torch.cat([mean_support, y_hat_slice], dim=1)
            lrp = self.lrp_transforms_prog[current_index](lrp_support)
            lrp = 0.5 * torch.tanh(lrp)
            y_hat_slice += lrp

            y_hat_slice = self.merge(y_hat_slice,y_hat_slices[current_index])

            y_hat_slices_quality.append(y_hat_slice)

        y_hat_en = torch.cat(y_hat_slices_quality,dim = 1)


        end_t = time.time()
        time_ = end_t - start_t

        if self.multiple_decoder:
            x_hat = self.base_net.g_s[1](y_hat_en).clamp_(0, 1)
        else:
            x_hat = self.g_s(y_hat_en).clamp_(0, 1) 
        return {"x_hat": x_hat,"y_hat":y_hat_en,"time":time_}          
