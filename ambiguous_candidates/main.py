# -*- coding: utf-8 -*-

import argparse

from datetime import datetime
from re import I
from transformers import BertTokenizer, AdamW, get_linear_schedule_with_warmup, get_constant_schedule_with_warmup

from torch.utils.data import DataLoader, TensorDataset
from torch.utils.tensorboard import SummaryWriter

from ambiguous_candidates_evaluation import evaluate_ambiguous_candidates
from model import *
from dataloader import *
from util import *

def train(args, train_dataset_1, train_dataset_2, dev_dataset, dev_source_data, model):
    tb_writer = SummaryWriter(args.output_dir)

    tr_sampler_1 = RandomSampler(train_dataset_1)
    tr_loader_1 = DataLoader(train_dataset_1, batch_size=args.train_batch_size, sampler=tr_sampler_1)
    tr_sampler_2 = RandomSampler(train_dataset_2)
    tr_loader_2 = DataLoader(train_dataset_2, batch_size=args.train_batch_size, sampler=tr_sampler_2)

    batch_nums = {str(task): len(loader) for task, loader in enumerate([tr_loader_1, tr_loader_2], start=1) if str(task) in args.task}
    task_total_batch_num = sum([batch_nums[task] for task in batch_nums])
    
    t_total = task_total_batch_num // args.gradient_accumulation_steps * args.num_train_epochs

    no_decay = ["bias", "LayerNorm.weight"]
    optimizer_grouped_parameters = [
        {
            "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
            "weight_decay": args.weight_decay,
        },
        {"params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)], "weight_decay": 0.0},
    ]

    optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon)
    scheduler = get_linear_schedule_with_warmup(
        optimizer, num_warmup_steps=args.warmup_step, num_training_steps=t_total
    )

    logging.info("***** Running training *****")
    logging.info("  Num examples = %d", sum([len(dataset) for task, dataset in enumerate([train_dataset_1, train_dataset_2], start=1) if str(task) in args.task]))
    logging.info("  Num Epochs = %d", args.num_train_epochs)
    logging.info(
        "  Total train batch size (w. parallel, accumulation) = %d",
        args.train_batch_size
        * args.gradient_accumulation_steps
    )
    logging.info("  Gradient Accumulation steps = %d", args.gradient_accumulation_steps)
    logging.info("  Total optimization steps = %d", t_total)
    logging.info("  Seed = %d", args.seed)

    best_f1 = 0.0
    best_precision = 0.0
    best_recall = 0.0
    best_f1_epoch = -1
    tr_loss = 0.0
    global_step = 0
    model.to(args.device)
    model.zero_grad()
    eval_metrics = eval(args, dev_dataset, dev_source_data, model)
    for epoch in trange(int(args.num_train_epochs), desc="Epoch..."):
        model.train()
        batch_nums = {str(task): len(loader) for task, loader in enumerate([tr_loader_1, tr_loader_2], start=1) if str(task) in args.task}
        order = order_selection([task for task in args.task], batch_nums)
        task_iterators = {str(task): iter(loader) for task, loader in enumerate([tr_loader_1, tr_loader_2], start=1) if str(task) in args.task}
        tr_loss = {task: .0 for task in task_iterators}
        nb_tr_examples = {task: 0 for task in task_iterators}
        for step, (cur_batch_task, index) in enumerate(tqdm(order, desc="Iteration over multi-tasks")):
            batch = next(task_iterators[cur_batch_task])
            batch = tuple(t.to(args.device) for t in batch)
            if cur_batch_task == "1":
                inputs = {
                    "input_ids": batch[0], 
                    "attention_mask": batch[1], 
                    "token_type_ids": batch[2], 
                    "scene_embed": batch[3],
                    "obj_embed": batch[4],
                    "labels": batch[5],
                    "obj_bbox": batch[6],
                    "obj_input_ids": batch[7],
                    "obj_attention_mask": batch[8],
                    "obj_token_type_ids": batch[9],
                    "task": cur_batch_task
                }
            elif cur_batch_task == "2":
                inputs = {
                    "input_ids": batch[0], 
                    "attention_mask": batch[1], 
                    "token_type_ids": batch[2], 
                    "scene_embed": batch[3],
                    "obj_embed": batch[4],
                    "BCE_labels": batch[5],
                    "obj_bbox": batch[6],
                    "obj_input_ids": batch[7],
                    "obj_attention_mask": batch[8],
                    "obj_token_type_ids": batch[9],
                    "task": cur_batch_task
                }
            else:
                raise TypeError

            outputs = model(**inputs)
            
            loss = outputs[1]

            if args.gradient_accumulation_steps > 1:
                loss = loss / args.gradient_accumulation_steps

            loss.backward()
            tr_loss[cur_batch_task] += loss.item()
            nb_tr_examples[cur_batch_task] += batch[0].size(0)

            if (step + 1) % args.gradient_accumulation_steps == 0:
                tb_writer.add_scalar('%s_training_loss' % cur_batch_task, loss.item(), global_step)
                torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)
                optimizer.step()
                scheduler.step()  # Update learning rate schedule
                model.zero_grad()
                global_step += 1

        logging.info('********** Train Result **********')
        logging.info('Epoch / Total Epoch : {} / {}'.format(epoch + 1, args.num_train_epochs))
        for task in tr_loss:
            logging.info('Task {} Loss : {:.4f}'.format(task, tr_loss[task] / nb_tr_examples[task]))

        eval_metrics = eval(args, dev_dataset, dev_source_data, model)
        for k, v in eval_metrics.items():
            tb_writer.add_scalar('Eval {}'.format(k), v, global_step)
        if epoch == 0:
            logging.info("MODEL saved at epoch {}".format(epoch + 1))
            torch.save(model.state_dict(), os.path.join(args.output_dir, "pytorch_model.bin"))
            best_f1 = eval_metrics['f1']
            best_precision = eval_metrics['precision']
            best_recall = eval_metrics['recall']
            best_f1_epoch = epoch + 1
        else:
            if eval_metrics['f1'] > best_f1:
                logging.info("Best F1 MODEL saved at epoch {}".format(epoch + 1))
                torch.save(model.state_dict(), os.path.join(args.output_dir, "pytorch_model.bin"))
                best_f1 = eval_metrics['f1']
                best_precision = eval_metrics['precision']
                best_recall = eval_metrics['recall']
                best_f1_epoch = epoch + 1

    logging.info('********** Train Result **********')
    logging.info(" Best F1. = {} / {}".format(best_f1, best_f1_epoch))
    logging.info(" Best Precision. = {} / {}".format(best_precision, best_f1_epoch))
    logging.info(" Best Recall. = {} / {}".format(best_recall, best_f1_epoch))

    tb_writer.close()

def eval(args, eval_dataset, source_data, model, submission=False):

    eval_sampler = SequentialSampler(eval_dataset)
    eval_dataloader = DataLoader(eval_dataset, batch_size=args.eval_batch_size, sampler=eval_sampler)

    logging.info("***** Running evaluation *****")
    logging.info("  Num examples = %d", len(eval_dataset))

    preds = defaultdict(list)
    model.to(args.device)
    model.eval()
    for batch in tqdm(eval_dataloader, desc="Test Evaluating"):
        batch = tuple(t.to(args.device) if type(t) == torch.Tensor else t for t in batch)

        inputs = {
            "input_ids": batch[0], 
            "attention_mask": batch[1], 
            "token_type_ids": batch[2],
            "scene_embed": batch[3],
            "obj_embed": batch[4],
            "obj_bbox": batch[8],
            "obj_input_ids": batch[9],
            "obj_attention_mask": batch[10],
            "obj_token_type_ids": batch[11],
            "task": "1"
        }

        with torch.no_grad():
            logits = model(**inputs).detach()

            nz_pred = torch.nonzero(logits[0] > 0)
            pred_list = nz_pred[:, 0].tolist()
            preds[batch[5].cpu().item()].append(
                {
                    "turn_id": batch[6].cpu().item(),
                    "disambiguation_candidates": [batch[7][0, p].cpu().item() for p in pred_list]
                }
            )

    if not submission:
        results = [
            {
                "dialog_id": dialog_id,
                "predictions": predictions,
            }
            for dialog_id, predictions in preds.items()
        ]
        eval_metrics = evaluate_ambiguous_candidates(source_data, results)
    
        logging.info('********** Eval Result **********')
        for k, v in eval_metrics.items():
            logging.info("{}: {:.4f}".format(k, v))
        return eval_metrics
    else:
        results = list()
        ambiguous_label = defaultdict(list)
        for d in source_data['data']:
            if d["ambiguous_label"] == 0:
                ambiguous_label[d["dialog_id"]].append(
                    {
                        "turn_id": d["turn_id"],
                        "disambiguation_candidates": [],
                    }
                )
        for dialog_id in ambiguous_label:
            results.append(
                {
                    "dialog_id": dialog_id,
                    "predictions": sorted(ambiguous_label[dialog_id] + preds[dialog_id], key=lambda x: x['turn_id']),
                }
            )

        return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data_dir",
        default='../data/',
        type=str,
        help="The input data dir.",
    )
    parser.add_argument(
        "--output_dir",
        default='./outputs',
        type=str,
        help="The output directory where the model predictions and checkpoints will be written.",
    )
    parser.add_argument(
        "--bert_model",
        default='Luyu/co-condenser-wiki',
        type=str,
        help="Pre-trained BERT model name"
    )
    parser.add_argument(
        "--vit_model",
        default='google/vit-base-patch16-224-in21k',
        type=str,
        help="Pre-trained Vit model name"
    )
    parser.add_argument(
        "--max_turns",
        default=3,
        type=int,
        help="The maximum total input turns."
    )
    parser.add_argument(
        "--do_train",
        action="store_true",
        help="Whether to run training."
    )
    parser.add_argument(
        "--do_eval",
        action="store_true",
        help="Whether to run eval on the test set."
    )
    parser.add_argument(
        "--train_batch_size",
        default=16,
        type=int,
        help="Batch size per GPU/CPU for training."
    )
    parser.add_argument(
        "--eval_batch_size",
        default=1,
        type=int,
        help="Batch size per GPU/CPU for evaluation."
    )
    parser.add_argument(
        "--no_cuda",
        action="store_true",
        help="Avoid using CUDA when available"
    )
    parser.add_argument(
        "--seed",
        default=42,
        type=int,
        help="Random seed for initialization"
    )
    parser.add_argument(
        "--learning_rate",
        default=5e-5,
        type=float,
        help="The initial learning rate for Adam."
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        default=1,
        type=int,
        help="Gradient accumulation steps size"
    )
    parser.add_argument(
        "--weight_decay",
        default=0.01,
        type=float,
        help="Weight decay if we apply some."
    )
    parser.add_argument(
        "--max_grad_norm",
        default=1.0,
        type=float,
        help="Max gradient norm."
    )
    parser.add_argument(
        "--num_train_epochs",
        default=10,
        type=int,
        help="Total number of training epochs to perform."
    )
    parser.add_argument(
        "--warmup_step",
        default=0,
        type=int,
        help="step of linear warmup"
    )
    parser.add_argument(
        "--adam_epsilon",
        default=1e-8,
        type=float,
        help="Epsilon for Adam optimizer."
    )
    parser.add_argument(
        "--negative_sample_size",
        default=10,
        type=int,
        help="Contrastive Loss negative sample size"
    )
    parser.add_argument(
        "--submission",
        action="store_true",
        help="Whether to run submission mode"
    )
    parser.add_argument(
        "--positive_weight",
        default=3.0,
        type=float,
        help="BCEWithLogitsLoss positive weight"
    )
    parser.add_argument(
        "--temperature",
        default=1.0,
        type=float,
        help="Contrastive Loss temperature"
    )
    parser.add_argument(
        "--task",
        default="12",
        type=str,
        help="1: ContrastiveLoss, 2: BCEWithLogitsLoss,"
    )
    args = parser.parse_args()
    args.task = list(args.task)

    # GPU device setting 
    args.device = torch.device('cuda' if torch.cuda.is_available() and not args.no_cuda else 'cpu')
    args.n_gpu = torch.cuda.device_count() if torch.cuda.is_available() and not args.no_cuda else 0

    args.now = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    if args.do_train and not os.path.exists(os.path.join(args.output_dir, args.now)):
        os.makedirs(os.path.join(args.output_dir, args.now))
        args.output_dir = os.path.join(args.output_dir, args.now)

    logging.basicConfig(
        format='%(asctime)s - %(levelname)s - %(message)s',
        level=logging.INFO,
        datefmt='%Y-%m-%d %H:%M:%S',
        filename='{}/log.log'.format(args.output_dir) if not os.path.exists(os.path.join(args.output_dir, 'log.log')) else '{}/log_2.log'.format(args.output_dir),
        filemode='a'
    )
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    console.setFormatter(formatter)
    logging.getLogger('').addHandler(console)

    logging.info("Arguments %s", args)
    logging.info("Device : {}  |  # of GPU : {}".format(args.device, args.n_gpu))
    
    set_seed(args)
    tokenizer = BertTokenizer.from_pretrained(args.bert_model)
    special_tokens_dict = {'additional_special_tokens': ['<USER>','<SYS>']}
    tokenizer.add_special_tokens(special_tokens_dict)
    model = Disambiguation_Detection(args)
    model.bert.resize_token_embeddings(len(tokenizer))
    model.to(args.device)

    if args.do_train:
        if os.path.exists(os.path.join("./data_cache", "train_features_1.bin")):
            train_features_1 = torch.load(os.path.join("./data_cache", "train_features_1.bin"))
        else:
            train_samples = load_data(os.path.join(args.data_dir, "simmc2.1_ambiguous_candidates_dstc11_train.json"))
            train_features_1 = convert_samples_to_features(args, train_samples, tokenizer, task="1")
            torch.save(train_features_1, os.path.join("./data_cache", "train_features_1.bin"))

        if os.path.exists(os.path.join("./data_cache", "train_features_2.bin")):
            train_features_2 = torch.load(os.path.join("./data_cache", "train_features_2.bin"))
        else:
            train_samples = load_data(os.path.join(args.data_dir, "simmc2.1_ambiguous_candidates_dstc11_train.json"))
            train_features_2 = convert_samples_to_features(args, train_samples, tokenizer, task="2")
            torch.save(train_features_2, os.path.join("./data_cache", "train_features_2.bin"))

        if os.path.exists(os.path.join("./data_cache", "dev_features.bin")):
            dev_features = torch.load(os.path.join("./data_cache", "dev_features.bin"))
        else:
            dev_samples = load_data(os.path.join(args.data_dir, "simmc2.1_ambiguous_candidates_dstc11_dev.json"))
            dev_features = convert_samples_to_features(args, dev_samples, tokenizer, eval=True)
            torch.save(dev_features, os.path.join("./data_cache", "dev_features.bin"))

        with open(os.path.join(args.data_dir, "simmc2.1_dials_dstc11_dev.json"), 'r', encoding='utf-8') as f:
            dev_source_data = json.load(f)

        if os.path.exists(os.path.join(args.data_dir, "image_features", "obj_image_train.bin")):
            obj_embed = torch.load(os.path.join(args.data_dir, "image_features", "obj_image_train.bin"))
        else:
            raise ValueError('Check your obj_image_train.bin file')
        if os.path.exists(os.path.join(args.data_dir, "image_features", "scene_image_train.bin")):
            scene_embed = torch.load(os.path.join(args.data_dir, "image_features", "scene_image_train.bin"))
        else:
            raise ValueError('Check your scene_image_train.bin file')
        if os.path.exists(os.path.join(args.data_dir, "image_features", "obj_image_train_normalized_bbox.bin")):
            obj_bbox = torch.load(os.path.join(args.data_dir, "image_features", "obj_image_train_normalized_bbox.bin"))
        else:
            raise ValueError('Check your obj_image_train_normalized_bbox.bin file')
        if os.path.exists(os.path.join(args.data_dir, "image_features", "obj_text_feature_train.bin")):
            obj_text = torch.load(os.path.join(args.data_dir, "image_features", "obj_text_feature_train.bin"))
        else:
            raise ValueError('Check your obj_text_feature_train.bin file')

        train_dataset_1 = TrainDataset_1(args, train_features_1, obj_embed, scene_embed, obj_bbox, obj_text)
        train_dataset_2 = TrainDataset_2(args, train_features_2, obj_embed, scene_embed, obj_bbox, obj_text)
        dev_dataset = TestDataset(args, dev_features, 'dev')

        train(args, train_dataset_1, train_dataset_2, dev_dataset, dev_source_data, model)

    if args.do_eval:
        model = Disambiguation_Detection(args)
        model.bert.resize_token_embeddings(len(tokenizer))
        model.load_state_dict(torch.load(os.path.join(args.output_dir, "pytorch_model.bin")), strict=False)
        
        if not args.submission:
            if os.path.exists(os.path.join("./data_cache", "devtest_features.bin")):
                dev_features = torch.load(os.path.join("./data_cache", "devtest_features.bin"))
            else:
                devtest_samples = load_data(os.path.join(args.data_dir, "simmc2.1_ambiguous_candidates_dstc11_devtest.json"))
                dev_features = convert_samples_to_features(args, devtest_samples, tokenizer, eval=True)
                torch.save(dev_features, os.path.join("./data_cache", "devtest_features.bin"))
            with open(os.path.join(args.data_dir, "simmc2.1_dials_dstc11_devtest.json"), 'r', encoding='utf-8') as f:
                dev_source_data = json.load(f)
                
            devtest_dataset = TestDataset(args, dev_features, 'devtest')
            eval_metrics = eval(args, devtest_dataset, dev_source_data, model)
        else:
            if os.path.exists(os.path.join("./data_cache", "teststd_features.bin")):
                teststd_features = torch.load(os.path.join("./data_cache", "teststd_features.bin"))
            else:
                teststd_samples = load_data(os.path.join(args.data_dir, "simmc2.1_ambiguous_candidates_dstc11_teststd_prediction.json"), eval=True)
                teststd_features = convert_samples_to_features(args, teststd_samples, tokenizer)
                torch.save(teststd_features, os.path.join("./data_cache", "teststd_features.bin"))
            with open(os.path.join(args.data_dir, "simmc2.1_ambiguous_candidates_dstc11_teststd_prediction.json"), 'r', encoding='utf-8') as f:
                teststd_source_data = json.load(f)
            
            teststd_dataset = TestDataset(args, teststd_features, 'teststd')
            predictions = eval(args, teststd_dataset, teststd_source_data, model, submission=True)

            with open(os.path.join(args.data_dir, "simmc2.1_dials_dstc11_teststd_public.json"), 'r', encoding='utf-8') as f:
                teststd_data = json.load(f)

            predicted_teststd = {"dialogue_data": list()}
            for dialog, preds in zip(teststd_data["dialogue_data"], predictions):
                predicted_dialogue = {"dialogue": list()}
                for turn_data, pred in zip(dialog['dialogue'], preds['predictions']):
                    turn_data["transcript_annotated"] = {
                        "act": "",
                        "act_attributes": {
                            "slot_values": dict(),
                            "request_slots": list(),
                            "objects": list()
                        },
                        "disambiguation_candidates": pred["disambiguation_candidates"]
                    }
                    predicted_dialogue["dialogue"].append(turn_data)
                for key in dialog.keys():
                    if key != "dialogue_data":
                        predicted_dialogue[key] = dialog[key]

                predicted_teststd["dialogue_data"].append(predicted_dialogue)

            with open(os.path.join(args.output_dir, 'dstc11-simmc-teststd-pred-subtask-123.json'), 'w', encoding='utf-8') as f:
                json.dump(predicted_teststd, f, indent='\t')
    
if __name__ == "__main__":
    main()