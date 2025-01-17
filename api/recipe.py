#!/usr/local/autopkg/python

from functools import reduce
from typing import List, Dict

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, status

import config, utils
from db import models
from api import user, settings
from api.slack import bot, build_msg, send_msg
from execute import recipe_manager, recipe_runner


log = utils.log
config.load()
router = APIRouter(
	prefix = "/recipe",
	tags = ["recipe"],
	responses = settings.custom_responses
)


@router.get("s/", summary="Get all recipes", description="Get all recipes in the database.", 
	dependencies=[Depends(user.get_current_user)])
async def get_recipes():

	recipes = await models.Recipe_Out.from_queryset(models.Recipes.all())

	return { "total": len(recipes), "recipes": recipes }


@router.get("/id/{id}", summary="Get recipe by id", description="Get a recipe by its id.", 
	dependencies=[Depends(user.get_current_user)], response_model=models.Recipe_Out)
async def get_by_id(id: int):

	recipe_object = await models.Recipe_Out.from_queryset_single(models.Recipes.get(id=id))

	return recipe_object


@router.get("/recipe_id/{recipe_id}", summary="Get recipe by recipe_id", description="Get a recipe by its recipe_id.", 
	dependencies=[Depends(user.get_current_user)], response_model=models.Recipe_Out)
async def get_by_recipe_id(recipe_id: str):

	recipe_object = await models.Recipe_Out.from_queryset_single(models.Recipes.get(recipe_id=recipe_id))

	return recipe_object


@router.post("/", summary="Create a recipe", description="Create a recipe.", 
	dependencies=[Depends(user.verify_admin)], response_model=models.Recipe_Out)
async def create(recipe_object: models.Recipe_In = Body(..., recipe_object=Depends(models.Recipe_In))):

	created_recipe = await models.Recipes.create(**recipe_object.dict(exclude_unset=True, exclude_none=True))

	return await models.Recipe_Out.from_tortoise_orm(created_recipe)


@router.put("/id/{id}", summary="Update recipe by id", description="Update a recipe by id.", 
	dependencies=[Depends(user.verify_admin)], response_model=models.Recipe_Out)
async def update_by_id(id: int, recipe_object: models.Recipe_In = Depends(models.Recipe_In)):

	if type(recipe_object) != dict:
		recipe_object = recipe_object.dict(exclude_unset=True, exclude_none=True)

	await models.Recipes.filter(id=id).update(**recipe_object)

	return await models.Recipe_Out.from_queryset_single(models.Recipes.get(id=id))


@router.put("/recipe_id/{recipe_id}", summary="Update recipe by recipe_id", description="Update a recipe by recipe_id.", 
	dependencies=[Depends(user.verify_admin)], response_model=models.Recipe_Out)
async def update_by_recipe_id(recipe_id: str, 
	recipe_object: models.Recipe_In = Body(..., recipe_object=Depends(models.Recipe_In))):

	if type(recipe_object) != dict:
		recipe_object = recipe_object.dict(exclude_unset=True, exclude_none=True)

	await models.Recipes.filter(recipe_id=recipe_id).update(**recipe_object)

	return await models.Recipe_Out.from_queryset_single(models.Recipes.get(recipe_id=recipe_id))


@router.delete("/id/{id}", summary="Delete recipe by id", description="Delete a recipe by id.", 
	dependencies=[Depends(user.verify_admin)])
async def delete_by_id(id: int):

	delete_object = await models.Recipes.filter(id=id).delete()

	if not delete_object:
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recipe does not exist.")

	else:
		return { "result":  "Successfully deleted recipe id:  {}".format(id) }


@router.delete("/recipe_id/{recipe_id}", summary="Delete recipe by recipe_id", 
	description="Delete a recipe by recipe_id.", dependencies=[Depends(user.verify_admin)])
async def delete_by_recipe_id(recipe_id: str):

	delete_object = await models.Recipes.filter(recipe_id=recipe_id).delete()

	if not delete_object:
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recipe does not exist.")

	else:
		return { "result":  "Successfully deleted recipe id:  {}".format(recipe_id) }


@router.post("/error", summary="Handle recipe errors", 
	description="This endpoint is called when a recipe errors out during an autopkg run.", 
	dependencies=[Depends(user.verify_admin)])
async def recipe_error(recipe_id: str, error: str):

	# Create DB Entry in errors table
	error_message = await models.ErrorMessages.create( recipe_id=recipe_id )

	# Post Slack Message
	try:
		error_list = error.split(': ')
		error_dict = reduce(lambda x, y: {y:x}, error_list[::-1])

	except:
		error_dict = { recipe_id: error }

	results = await send_msg.recipe_error_msg(recipe_id, error_message.id, error_dict)

	updates = {
		"slack_ts": results.get('ts'),
		"slack_channel": results.get('channel')
	}

	await models.ErrorMessages.update_or_create(updates, id=error_message.id)

	# Mark the recipe disabled
	recipe_object = await models.Recipes.filter(recipe_id=recipe_id).first()
	recipe_object.enabled = False
	await recipe_object.save()

	return { "Result": "Success" }


# @router.post("/trust", summary="Update recipe trust info", 
@router.post("/trust/update", summary="Update recipe trust info", 
	description="Update a recipe's trust information.  Runs `autopkg update-trust-info <recipe_id>`.", 
	dependencies=[Depends(user.verify_admin)])
# async def trust_recipe(id: int, background_tasks: BackgroundTasks, user_id: str, channel: str):
async def recipe_trust_update(id: int, background_tasks: BackgroundTasks, user_id: str, channel: str):

	# Get Error ID
	error_object = await models.ErrorMessage_Out.from_queryset_single(models.ErrorMessages.get(id=id))

	# Get recipe object
	recipe_object = await models.Recipes.filter(recipe_id=error_object.recipe_id).first()

	# extra_args = { 'error_id': id }

	if recipe_object:
		background_tasks.add_task( 
			recipe_runner.main,
			[
				"--recipe-identifier", error_object.recipe_id,
				"--action", "trust",
				# str(extra_args)
				"--error_id", id
			] 
		)

		# Mark the recipe enabled
		recipe_object.enabled = True
		await recipe_object.save()

		return { "Result": "Queued background task..." }

	else:

		blocks = await build_msg.missing_recipe_msg(error_object.recipe_id, "update trust for")

		await bot.SlackBot.post_ephemeral_message(
			user_id, blocks, 
			channel=channel, 
			text="Encountered error attempting to update trust for `{}`".format(error_object.recipe_id)
		)


# @router.post("/do-not-trust", summary="Do not approve trust changes", 
@router.post("/trust/deny", summary="Do not approve trust changes", 
	description="This endpoint will update that database to show that the "
		"changes to parent recipe(s) were not approved.", 
	dependencies=[Depends(user.verify_admin)])
# async def disapprove_changes(id: int):
async def recipe_trust_deny(id: int):

	# Get Error ID
	error_object = await models.ErrorMessage_Out.from_queryset_single(models.ErrorMessages.get(id=id))

	await send_msg.deny_trust_msg(error_object)


# @router.post("/trust-update-success", summary="Trust info was updated successfully", 
@router.post("/trust/update/success", summary="Trust info was updated successfully", 
	description="Performs the necessary actions after trust info was successfully updated.", 
	dependencies=[Depends(user.verify_admin)])
# async def trust_update_success(recipe_id: str, msg: str):
async def recipe_trust_update_success(recipe_id: str, msg: str, error_id: int):
	""" When update-trust-info succeeds """

	# results = await models.ErrorMessage_Out.from_queryset(models.ErrorMessages.filter(recipe_id=recipe_id))
	# Get DB Entry
	error_object = await models.ErrorMessage_Out.from_queryset_single(models.ErrorMessages.get(id=error_id))

	# Hacky work around if there are multiple "error messages" in the database for the recipe id
	# while not results[-1].response_url:
	# 	del results[-1]

	return await send_msg.update_trust_success_msg(error_object)


# @router.post("/trust-update-error", summary="Trust info failed to update", 
@router.post("/trust/update/failed", summary="Trust info failed to update", 
	description="Performs the necessary actions after trust info failed to update.", 
	dependencies=[Depends(user.verify_admin)])
# async def trust_update_error(recipe_id: str, msg: str): #, 
async def recipe_trust_update_failed(recipe_id: str, msg: str):
	""" When update-trust-info fails """

	# Get DB Entry
	error_object = await models.ErrorMessage_Out.from_queryset_single(models.ErrorMessages.get(recipe_id=recipe_id))

	results = await send_msg.update_trust_error_msg(msg, error_object)

	updates = {
		"slack_ts": results.get('ts'),
		"slack_channel": results.get('channel')
	}

	await models.ErrorMessages.update_or_create(updates, id=error_object.id)

	# Mark the recipe disabled
	recipe_object = await models.Recipes.filter(recipe_id=error_object.recipe_id).first()
	recipe_object.enabled = False
	await recipe_object.save()

	return { "Result": "Success" }


# @router.post("/trust-verify-error", summary="Parent trust info has changed", 
@router.post("/trust/verify/failed", summary="Parent trust info has changed", 
	description="Performs the necessary actions after parent recipe trust info has changed.", 
	dependencies=[Depends(user.verify_admin)])
# async def trust_error(payload: dict = Body(...)):
async def reciepe_trust_verify_failed(payload: dict = Body(...)):
	""" When `autopkg verify-trust-info <recipe_id>` fails """

	recipe_id = payload.get("recipe_id")
	error_msg = payload.get("msg")

	# Create DB Entry in errors table
	error_object = await models.ErrorMessages.create(recipe_id=recipe_id)

	# Post Slack Message
	results = await send_msg.trust_diff_msg(error_msg, error_object)

	# Mark the recipe disabled
	recipe_object = await models.Recipes.filter(recipe_id=error_object.recipe_id).first()
	recipe_object.enabled = False
	await recipe_object.save()

	return { "Result": "Success" }
